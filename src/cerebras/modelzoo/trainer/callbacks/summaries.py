# Copyright 2022 Cerebras Systems.
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

"""Contains utilities for summarizing scalars and tensors."""

from contextlib import contextmanager

from cerebras.modelzoo.trainer.callbacks import (
    Callback,
    register_global_callback,
)


class _LogSummaries(Callback):
    """Callback class caches the trainer instance for summarizing through methods below."""

    def __init__(self):
        self._trainer_stack = []

    @property
    def trainer(self):
        if self._trainer_stack:
            return self._trainer_stack[-1]
        return None

    def on_enter_train(
        self,
        trainer,
        stack,
        train_dataloader,
        loop,
        loop_idx,
    ):
        stack.enter_context(self._cache_trainer(trainer))

    def on_enter_validate(self, trainer, stack, val_dataloader, loop):
        stack.enter_context(self._cache_trainer(trainer))

    def on_enter_validate_all(self, trainer, stack, val_dataloaders, loop):
        stack.enter_context(self._cache_trainer(trainer))

    @contextmanager
    def _cache_trainer(self, trainer):
        try:
            self._trainer_stack.append(trainer)
            yield
        finally:
            self._trainer_stack.pop()


_GLOBAL_SUMMARIES = _LogSummaries()
register_global_callback(_GLOBAL_SUMMARIES)


def summarize_scalar(name, value):
    """Log scalar values to the trainer loggers.

    Args:
        name: The name of the metric.
        value: Scalar value of the metric to log.
    """
    if _GLOBAL_SUMMARIES.trainer:
        _GLOBAL_SUMMARIES.trainer.log_metrics(**{name: value})
    else:
        import cerebras.pytorch.utils.tensorboard as tb

        tb.summarize_scalar(name, value)


def summarize_tensor(name, value):
    """Log tensor values to the trainer loggers.

    Args:
        name: The name of the metric.
        value: Tensor value of the metric to log.
    """
    if _GLOBAL_SUMMARIES.trainer:
        _GLOBAL_SUMMARIES.trainer.log_metrics(**{name: value})
    else:
        import cerebras.pytorch.utils.tensorboard as tb

        tb.summarize_tensor(name, value)
