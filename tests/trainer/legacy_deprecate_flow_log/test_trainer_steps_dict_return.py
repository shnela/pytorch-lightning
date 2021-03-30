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
"""
Tests to ensure that the training loop works with a dict
"""

from pytorch_lightning import Trainer
from tests.helpers.deterministic_model import DeterministicModel


def test_training_step_dict(tmpdir):
    """
    Tests that only training_step can be used
    """
    model = DeterministicModel()
    model.training_step = model.training_step__dict_return
    model.val_dataloader = None

    trainer = Trainer(
        default_root_dir=tmpdir,
        fast_dev_run=True,
        weights_summary=None,
    )
    trainer.fit(model)

    # make sure correct steps were called
    assert model.training_step_called
    assert not model.training_step_end_called
    assert not model.training_epoch_end_called

    # make sure training outputs what is expected
    for batch_idx, batch in enumerate(model.train_dataloader()):
        break

    out = trainer.train_loop.run_training_batch(batch, batch_idx, 0)

    assert out.signal == 0
    assert trainer.logger_connector.logged_metrics['log_acc1'] == 12.0
    assert trainer.logger_connector.logged_metrics['log_acc2'] == 7.0

    train_step_out = out.training_step_output_for_epoch_end
    assert len(train_step_out) == 1

    train_step_out = train_step_out[0][0]
    pbar_metrics = train_step_out['progress_bar']
    assert 'log' in train_step_out
    assert 'progress_bar' in train_step_out
    assert train_step_out['train_step_test'] == 549
    assert pbar_metrics['pbar_acc1'] == 17.0
    assert pbar_metrics['pbar_acc2'] == 19.0

    # make sure the optimizer closure returns the correct things
    opt_closure_result = trainer.train_loop.training_step_and_backward(
        batch, batch_idx, 0, trainer.optimizers[0], trainer.hiddens
    )
    assert opt_closure_result['loss'] == (42.0 * 3) + (15.0 * 3)


def training_step_with_step_end(tmpdir):
    """
    Checks train_step + training_step_end
    """
    model = DeterministicModel()
    model.training_step = model.training_step__dict_return
    model.training_step_end = model.training_step_end__dict
    model.val_dataloader = None

    trainer = Trainer(
        default_root_dir=tmpdir,
        fast_dev_run=True,
        weights_summary=None,
    )
    trainer.fit(model)

    # make sure correct steps were called
    assert model.training_step_called
    assert model.training_step_end_called
    assert not model.training_epoch_end_called

    # make sure training outputs what is expected
    for batch_idx, batch in enumerate(model.train_dataloader()):
        break

    out = trainer.train_loop.run_training_batch(batch, batch_idx, 0)
    assert out.signal == 0
    assert trainer.logger_connector.logged_metrics['log_acc1'] == 14.0
    assert trainer.logger_connector.logged_metrics['log_acc2'] == 9.0

    train_step_end_out = out.training_step_output_for_epoch_end
    pbar_metrics = train_step_end_out['progress_bar']
    assert 'train_step_end' in train_step_end_out
    assert pbar_metrics['pbar_acc1'] == 19.0
    assert pbar_metrics['pbar_acc2'] == 21.0
