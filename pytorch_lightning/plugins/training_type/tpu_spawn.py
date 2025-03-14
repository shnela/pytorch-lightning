# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import io
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Union

import torch
import torch.multiprocessing as mp

from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning.plugins.training_type.ddp_spawn import DDPSpawnPlugin
from pytorch_lightning.plugins.training_type.utils import on_colab_kaggle
from pytorch_lightning.trainer.states import TrainerState
from pytorch_lightning.utilities import _TPU_AVAILABLE, rank_zero_warn
from pytorch_lightning.utilities.distributed import rank_zero_only, ReduceOp
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.seed import seed_everything

if _TPU_AVAILABLE:
    import torch_xla.core.xla_model as xm
    import torch_xla.distributed.parallel_loader as xla_pl
    import torch_xla.distributed.xla_multiprocessing as xmp
    from torch_xla.core.xla_model import rendezvous
    from torch_xla.distributed.parallel_loader import ParallelLoader
else:
    xm, xla_pl, xmp, ParallelLoader, rendezvous = [None] * 5


class TPUSpawnPlugin(DDPSpawnPlugin):

    def __init__(
        self,
        parallel_devices: Optional[List[torch.device]] = None,
        num_nodes: int = 1,
        **kwargs: Dict[str, Any]
    ) -> None:
        super().__init__(
            parallel_devices, num_nodes=num_nodes, cluster_environment=None, sync_batchnorm=False, **kwargs
        )
        self.tpu_local_core_rank = 0
        self.start_method = None

    def setup(self, model: torch.nn.Module) -> torch.nn.Module:
        self.create_mp_queue()
        return self.model

    def create_mp_queue(self):
        self.start_method = 'fork'
        smp = mp.get_context(self.start_method)
        self.mp_queue = smp.SimpleQueue()

    @property
    def distributed_sampler_kwargs(self) -> dict:
        return dict(num_replicas=xm.xrt_world_size(), rank=xm.get_ordinal())

    @property
    def is_distributed(self):
        return self.world_size != 1

    def process_dataloader(self, dataloader: Union[Iterable, torch.utils.data.DataLoader]) -> ParallelLoader:
        device = xm.xla_device()
        dataloader = xla_pl.ParallelLoader(dataloader, [device])
        dataloader = dataloader.per_device_loader(device)
        return dataloader

    def configure_ddp(self) -> None:
        pass

    def init_ddp_connection(self, global_rank: int, world_size: int) -> None:
        pass

    def set_world_ranks(self, process_idx: int) -> None:
        self.tpu_local_core_rank = xm.get_local_ordinal()
        self.tpu_global_core_rank = xm.get_ordinal()
        self.global_rank = self.tpu_local_core_rank
        self.world_size = self.num_nodes * self.num_processes

    def new_process(self, process_idx: int, trainer, mp_queue) -> None:
        self.mp_queue = mp_queue

        seed = os.environ.get("PL_GLOBAL_SEED")
        if seed is not None:
            seed_everything(int(seed))

        self.set_world_ranks(process_idx)

        # set warning rank
        rank_zero_only.rank = self.global_rank

        if self.tpu_global_core_rank != 0 and trainer.progress_bar_callback is not None:
            trainer.progress_bar_callback.disable()

        self.model_to_device()
        trainer.accelerator.setup_optimizers(trainer)
        trainer.precision_plugin.connect(self._model, None, None)

        self.barrier("pre-run-stage")

        results = trainer.run_stage()

        self.__save_end_of_training_weights(self.lightning_module)
        self.transfer_distrib_spawn_state_on_fit_end(results)

        self.barrier("end-process")

    def __save_end_of_training_weights(self, model: LightningModule) -> None:
        # when training ends on these platforms dump weights to get out of the main process
        if on_colab_kaggle():
            rank_zero_warn("cleaning up... please do not interrupt")
            self.save_spawn_weights(model)

    def model_to_device(self) -> None:
        self._model.to(xm.xla_device())

    def barrier(self, name: Optional[str] = None) -> None:
        rendezvous(name)

    def transfer_distrib_spawn_state_on_fit_end(self, results):
        checkpoint_callback = self.lightning_module.trainer.checkpoint_callback
        best_model_path = checkpoint_callback.best_model_path if checkpoint_callback else None

        if self.mp_queue is not None:
            rank_zero_warn("cleaning up ddp environment...")

            # save the last weights
            last_path = None
            if (
                self.lightning_module.trainer.state == TrainerState.FITTING and best_model_path is not None
                and len(best_model_path) > 0
            ):
                last_path = re.sub(".ckpt", ".tmp_end.ckpt", best_model_path)
                self.save(self.lightning_module.state_dict(), last_path)

            if self.global_rank == 0:
                # todo, pass complete checkpoint as state dictionary
                self.mp_queue.put(best_model_path)
                self.mp_queue.put(last_path)
                self.mp_queue.put(results)

    def save(self, state_dict: Dict, path: str) -> None:
        """
        Saving with ``xm.save`` can be unstable and miss the rendez-vous after ``torch.save``.
        The rendez-vous doesn't affect directly saving.
        We can ignore the ``RuntimeError`` to reduce friction with TPUs.
        """
        try:
            xm.save(state_dict, path)
        except RuntimeError as e:
            if "Failed to meet rendezvous" not in str(e):
                raise e

    def broadcast(self, obj: object, src: int = 0) -> object:
        buffer = io.BytesIO()
        torch.save(obj, buffer)
        data = bytearray(buffer.getbuffer())
        data_tensor = torch.tensor(data).to(xm.xla_device(), dtype=torch.float)
        data = xm.all_gather(data_tensor)
        buffer = io.BytesIO(data.cpu().byte().numpy())
        obj = torch.load(buffer)
        return obj

    def load_spawn_weights(self, original_model: LightningModule) -> LightningModule:
        """
        Load the temp weights saved in the process
        To recover the trained model from the ddp process we load the saved weights
        """

        loaded_model = original_model

        if self.is_global_zero:
            # load weights saved in ddp
            path = os.path.join(original_model.trainer.default_root_dir, "__temp_weight_distributed_end.ckpt")
            loaded_model = original_model.__class__.load_from_checkpoint(path)

            # copy loaded weights to old model
            original_model.load_state_dict(loaded_model.state_dict())

            # remove ddp weights
            os.remove(path)

        return loaded_model

    def save_spawn_weights(self, model: LightningModule) -> Optional[str]:
        """
        Dump a temporary checkpoint after ddp ends to get weights out of the process
        """
        if model.trainer.is_global_zero:
            path = os.path.join(model.trainer.default_root_dir, "__temp_weight_distributed_end.ckpt")
            model.trainer.save_checkpoint(path)
            return path

    def reduce_decision(self, decision: bool) -> bool:
        decision = torch.tensor(int(decision), device=self.device)
        decision = self.reduce(decision, "sum")
        decision = bool(decision == self.world_size)
        return decision

    def reduce(self, output, group: Optional[Any] = None, reduce_op: Optional[Union[ReduceOp, str]] = None):
        if not isinstance(output, torch.Tensor):
            output = torch.tensor(output, device=self.device)

        _invalid_reduce_op = isinstance(reduce_op, ReduceOp) and reduce_op != ReduceOp.SUM
        _invalid_reduce_op_str = isinstance(reduce_op, str) and reduce_op.lower() not in ("sum", "mean", "avg")
        if _invalid_reduce_op or _invalid_reduce_op_str:
            raise MisconfigurationException(
                "Currently, TPUSpawn TrainingTypePlugin only support `sum`, `mean`, `avg` reduce operation."
            )

        output = xm.mesh_reduce('reduce', output, sum)

        if isinstance(reduce_op, str) and reduce_op.lower() in ("avg", "mean"):
            output = output / self.world_size

        return output

    def post_dispatch(self) -> None:
        # TODO: Check if trainer references can be resolved otherwise
        model = self.lightning_module

        # restore main state with best weights
        best_path = self.mp_queue.get()
        last_path = self.mp_queue.get()
        self._results = self.mp_queue.get()

        # transfer back the best path to the trainer
        if self.lightning_module.trainer.checkpoint_callback is not None:
            self.lightning_module.trainer.checkpoint_callback.best_model_path = best_path
        # todo, pass also bets score

        # load last weights
        if last_path and model.trainer.state == TrainerState.FITTING:
            ckpt = torch.load(last_path, map_location=lambda storage, loc: storage)
            model.load_state_dict(ckpt)

        self._model = model

        # when training completes, load the weights back in main process
        self.__load_weights_on_main_process()

    def __load_weights_on_main_process(self) -> None:
        model = self.lightning_module

        # load weights if not interrupted
        if on_colab_kaggle() and model.trainer.state == TrainerState.FITTING:
            self.load_spawn_weights(model)

        self._model = model

    def _close_logger(self, trainer) -> None:
        if trainer.logger is not None:
            trainer.logger.finalize("success")

    @property
    def xmp_spawn_kwargs(self):
        return {
            "args": (self.lightning_module.trainer, self.mp_queue),
            "nprocs": len(self.parallel_devices),
            "start_method": self.start_method
        }

    def start_training(self, trainer) -> None:
        # todo: precision pluging is call in accelerator setup and should be moved
        if 'XLA_USE_BF16' in os.environ:
            del os.environ["XLA_USE_BF16"]
        self._close_logger(trainer)
        xmp.spawn(self.new_process, **self.xmp_spawn_kwargs)

    def start_evaluating(self, trainer) -> None:
        self._close_logger(trainer)
        xmp.spawn(self.new_process, **self.xmp_spawn_kwargs)

    def start_predicting(self, trainer) -> None:
        xmp.spawn(self.new_process, **self.xmp_spawn_kwargs)

    def training_step(self, *args, **kwargs):
        return self.lightning_module.training_step(*args, **kwargs)

    def validation_step(self, *args, **kwargs):
        return self.lightning_module.validation_step(*args, **kwargs)

    def test_step(self, *args, **kwargs):
        return self.lightning_module.test_step(*args, **kwargs)

    def predict_step(self, *args, **kwargs):
        return self.lightning_module.predict_step(*args, **kwargs)

    def save_checkpoint(self, checkpoint: Dict[str, Any], filepath: str) -> None:
        """Save model/training states as a checkpoint file through state-dump and file-write.

        Args:
            checkpoint: dict containing model and trainer state
            filepath: write-target file's path
        """
        # Todo: TypeError: 'mappingproxy' object does not support item assignment
        self.save({k: v for k, v in checkpoint.items() if k != "callbacks"}, filepath)
