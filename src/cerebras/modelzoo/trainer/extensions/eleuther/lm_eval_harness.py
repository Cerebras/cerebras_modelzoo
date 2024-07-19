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

"""
This module provides a callback class to run EleutherAI's Evaluation Harness.
"""

from copy import deepcopy
from functools import cached_property
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
from lm_eval.api.instance import Instance
from lm_eval.api.model import LM
from lm_eval.api.registry import register_model

import cerebras.pytorch as cstorch
from cerebras.appliance.environment import appliance_environ
from cerebras.modelzoo.data.nlp.gpt.InferenceDataProcessor import (
    RequestType,
    tokenize_stop_words,
)
from cerebras.modelzoo.trainer.callbacks import (
    Callback,
    ValidationCallback,
    ValidationLoop,
)
from cerebras.modelzoo.trainer.callbacks.flags import _ScopedFlags
from cerebras.modelzoo.trainer.extensions.eleuther.eval_harness_utils import (
    EleutherCLIArgs,
    EvalHarnessRunner,
)
from cerebras.modelzoo.trainer.extensions.eval_harness_adapter import (
    CSEvalHarnessAdapter,
    EvalHarnessProgress,
)
from cerebras.pytorch.nn.functional import one_hot

CS_LLM = "cs-llm"


@register_model(CS_LLM)
class EleutherLM(CSEvalHarnessAdapter, LM):
    """Subclasses Eleuther's `LM` base class, overriding the `loglikelihood`
    and `generate_until` methods that are called from EEH's `evaluator.evaluate` method.
    """

    def __init__(self, trainer, dataloader_args: Dict[str, Any]):
        """
        Args:
            trainer: The Trainer object to use to run validation.
            dataloader_args: A dictionary consisting of arguments to pass to
                the dataloader.
        """
        LM.__init__(self)
        CSEvalHarnessAdapter.__init__(
            self, trainer=trainer, dataloader_args=dataloader_args
        )
        self.gen_kwargs: Optional[Dict[str, Any]] = None

        # pylint: disable=line-too-long
        # Dummy model attr needed for EEH script
        # Ref: https://github.com/EleutherAI/lm-evaluation-harness/blob/c9bbec6e7de418b9082379da82797522eb173054/lm_eval/evaluator.py#L165-L167
        self.model = lambda: None
        self.model.config = lambda: None
        self.model.config._name_or_path = CS_LLM

    def loglikelihood(
        self, requests: List[Instance]
    ) -> List[Tuple[float, bool]]:
        # pylint: disable=line-too-long
        """This method provides an implementation for the abstract method of
        `EEH's LM interface class <lm_eval_model>`_.

        .. _lm_eval_model: https://github.com/EleutherAI/lm-evaluation-harness/blob/c9bbec6e7de418b9082379da82797522eb173054/lm_eval/api/model.py#L34

        This method preprocesses the raw text requests, generates the data
        samples to be consumed by the GPT2 model, and executes the data on
        the appliance.

        Args:
            requests: A list of EEH's Instance objects, with property `args` which returns a tuple
            of (context, continuation) strings.

        Returns:
            list of size `len(requests)` comprising tuple of (float, bool) representing
            - logprob of generating the continuation string
            - whether `continuation` is generated by greedy sampling from `context`
        """
        (
            _,
            samples_file_list,
            dataset_size,
            token_lengths,
        ) = self.preprocess_dataset(requests, RequestType.eeh_loglikelihood)

        with LogLikelihood(token_lengths) as ll:
            self.trainer.validate(
                val_dataloader=cstorch.utils.data.DataLoader(
                    self.input_fn,
                    self.dataloader_args,
                    samples_file_list,
                    dataset_size,
                    RequestType.eeh_loglikelihood.value,
                ),
                loop=EleutherEvalHarnessLoop(),
                ckpt_path=None,
            )

            if (
                not self.trainer.backend.is_e2e_execution
            ):  # Dummy results for compile-only flow
                return [(-0.0, False)] * dataset_size

            self.logger.debug(f"Output results: {ll.results}")
            return ll.results

    def loglikelihood_rolling(self, requests):
        raise NotImplementedError(
            "Loglikelihood rolling is currently not supported"
        )

    def generate_until(self, requests: List[Instance]) -> List[str]:
        # pylint: disable=line-too-long
        """This method provides an implementation for the abstract method of
        `EEH's LM interface class <lm_eval_model>`_.

        .. _lm_eval_model: https://github.com/EleutherAI/lm-evaluation-harness/blob/c9bbec6e7de418b9082379da82797522eb173054/lm_eval/api/model.py#L102

        Args:
            requests: A list of EEH Instance objects with property `args` which returns a tuple
            of (context, until) strings

        Returns:
            list of size `len(requests)` comprising generated continuation strings
        """
        (
            tokenizer,
            samples_file_list,
            dataset_size,
            metadata,
        ) = self.preprocess_dataset(requests, RequestType.eeh_generate_until)

        until, _ = metadata[0]
        stop_sequences = tokenize_stop_words(
            stop_words=until, tokenizer=tokenizer
        )

        with GenerateUntil(
            tokenizer, metadata, stop_sequences, self.gen_kwargs
        ) as gen:
            self.trainer.validate(
                val_dataloader=cstorch.utils.data.DataLoader(
                    self.input_fn,
                    self.dataloader_args,
                    samples_file_list,
                    dataset_size,
                    RequestType.eeh_generate_until.value,
                ),
                loop=EleutherEvalHarnessLoop(),
                ckpt_path=None,
            )

            if (
                not self.trainer.backend.is_e2e_execution
            ):  # Dummy results for compile-only flow
                return [""] * dataset_size

            self.logger.debug(f"Output results: {gen.results}")
            return gen.results


class EleutherEvalHarnessLoop(ValidationLoop):
    """Subclass of `ValidationLoop` to run EleutherAI's Evaluation Harness."""

    def __init__(self):
        """Initializes the EleutherEvalHarnessLoop object."""
        super().__init__(hook="eleuther_eval_harness")

    def on_eleuther_eval_harness_start(
        self, trainer, model, val_dataloader, loop
    ):
        """
        Run ValidationLoop's `on_validate_start` method to ensure that
        eval_steps is being computed correctly.
        """
        model.eval()
        self.on_validate_start(trainer, model, val_dataloader, loop)


class LogLikelihood(Callback):
    """
    Callback class to post-process model output logits to calculate
    log probabilities and exact match for continuation tokens.
    """

    def __init__(self, token_lengths):
        """
        Args:
            token_lengths: List of tuples of (context_length, continuation_length)
                for each sample in the batch.
        """
        self.token_lengths = token_lengths
        self.sample_idx = 0
        self.results = []

        self.progress = EvalHarnessProgress("EleutherAI Eval")

    def on_before_forward(self, trainer, model, batch, args, kwargs):
        # TODO: We need something more generic than this. User model is not guaranteed to
        #       accept output_logits as a kwarg to its forward pass
        kwargs["output_logits"] = True

    def on_after_forward(self, trainer, model, outputs, batch):
        outputs.pop("loss", None)
        lm_logits = outputs.pop("logits")

        # Calculate softmax of logits
        lm_logits = torch.nn.functional.log_softmax(
            lm_logits.float(), dim=-1, dtype=torch.float32
        )

        # Post processing of output logits to produce
        # predictions and logits for continuation tokens
        attn_mask = batch["attention_mask"].to(torch.float32)
        cont_tokens = batch["continuation"].to(torch.long)

        # Only keep logits corresponding to the continuation token positions
        cont_logits = lm_logits.clone()
        # Step 1: repeat attn_mask vocab_size times along the 2nd dim
        # [bs, msl] -> [bs, msl, vocab_size]
        attn_mask = attn_mask.unsqueeze(2).repeat(1, 1, cont_logits.shape[-1])

        # Step 2: zero out the logits except the ones corresponding to continuation
        # token positions
        cont_logits = cont_logits * attn_mask

        # Step 3: gather probs corresponding to the tokens in continuation
        cont_toks_one_hot = one_hot(
            cont_tokens, num_classes=lm_logits.shape[-1]
        ).to(cont_logits.dtype)

        cont_logits = cont_logits * cont_toks_one_hot
        cont_log_probs = cont_logits.sum(-1)

        predictions = lm_logits.argmax(-1).int()
        # Subtract `cont_tokens` from `predictions` and output
        # comparisons tensor to check if the continuation token
        # predictions match the input
        cont_comparisons = torch.add(predictions * -1, cont_tokens)

        self.post_process(trainer, cont_comparisons, cont_log_probs)

        # Remove logits from outputs dictionary, so it doesn't
        # get marked as model output which is leading to
        # compile issues in some cases.
        outputs.pop("logits", None)

    def on_eleuther_eval_harness_batch_end(
        self, trainer, model, outputs, batch, batch_idx
    ):
        """Runs after every batch is processed."""
        self.progress.print(trainer, batch_idx)

    @cstorch.step_closure
    def post_process(self, trainer, cont_comparisons, log_probs):
        """
        Post-processes the model output logits to calculate log probabilities.

        Args:
            trainer: the Trainer object
            cont_comparisons: Tensor of shape (batch_size, max_seq_len)
                containing the comparison tensor for the continuation tokens
            log_probs: Tensor of shape (batch_size, max_seq_len)
                containing the log probabilities for the continuation tokens
        """
        trainer.logger.debug(
            f"Continuation Comparisons={cont_comparisons}, "
            f"Logits={log_probs}, "
        )

        # Post processing of model output to produce results
        for comparison, cont_logits in zip(cont_comparisons, log_probs):
            tok_lengths = self.token_lengths[self.sample_idx]
            ctx_len, cont_len = tok_lengths
            if not ctx_len or not cont_len:
                # Skip post processing for padded 0 tensors
                continue

            # Since we subtracted the model's predictions from the input
            # tokens, predictions exactly match the continuation tokens
            # where the `comparison` tensor has 0s
            cont_comparison = comparison[ctx_len - 1 : ctx_len + cont_len - 1]
            max_equal = (cont_comparison == 0).all()

            # Answer: (log prob, is-exact-match)
            answer = (float(cont_logits.sum()), bool(max_equal))

            self.results.append(answer)
            self.sample_idx += 1


class GenerateUntil(Callback):
    """
    Callback class to post-process model output logits to generate continuation
    strings until a specified token is generated.
    """

    def __init__(
        self,
        tokenizer,
        metadata: List[Tuple[str, int]],
        stop_sequences: List[List[int]],
        gen_kwargs: Dict[str, Any],
    ):
        """
        Args:
            tokenizer: Tokenizer object used to decode the generated continuation
            metadata: List of tuples of (until token sequences, ctx length)
                for each sample in the batch.
            stop_sequences: List of stop token sequences for stopping generation
            gen_kwargs: Dict specifying settings for generative inference.
        """
        self.tokenizer = tokenizer
        self.metadata = metadata
        self.start_token = None
        self.sample_idx = 0
        self.results = []
        self.original_max_act_per_csx = None

        # Generation settings
        self.until, _ = metadata[0]
        self.stop_sequences = stop_sequences
        self.temperature = gen_kwargs.get("temperature")
        self.top_p = gen_kwargs.get("top_p")
        self.top_k = gen_kwargs.get("top_k")
        self.max_tokens = gen_kwargs.get("max_length_generation")

        self.progress = EvalHarnessProgress("EleutherAI Generative Eval")

    def on_eleuther_eval_harness_start(
        self, trainer, model, val_dataloader, loop
    ):
        """Runs before the EleutherAI Evaluation Harness starts."""
        self.start_token = getattr(model, "start_token", None)

        if self.start_token is None:
            raise RuntimeError(
                "No start token specified under `model.start_token`. "
                "Please specify a start token for generative tasks."
            )

        model.stop_sequences = self.stop_sequences

        if self.max_tokens is not None:
            model.max_tokens = self.max_tokens

        if self.temperature is not None:
            model.temperature = self.temperature

        if self.top_p is not None:
            model.top_p = self.top_p

        if self.top_k is not None:
            model.top_k = self.top_k

    def on_eleuther_eval_harness_batch_end(
        self, trainer, model, outputs, batch, batch_idx
    ):
        """Runs after every batch is processed."""
        self.progress.print(trainer, batch_idx)

    def on_before_forward(self, trainer, model, batch, args, kwargs):
        kwargs["autoregressive"] = True

    def on_after_forward(self, trainer, model, outputs, batch):
        self.post_process(predictions=outputs["output"])

    @cstorch.step_closure
    def post_process(self, predictions):
        """
        Post-processes the model output logits to generate continuation strings.

        Args:
            predictions: Tensor of shape (batch_size, max_seq_len)
                containing the model's predictions
        """
        # Post processing of model output to produce results
        for pred in predictions:
            if not self.metadata[self.sample_idx]:
                # Skip post processing for padded 0 tensors
                continue
            _, ctx_len = self.metadata[self.sample_idx]

            # Get tokens for the generated continuation string
            gen_continuation = pred[ctx_len:].tolist()
            try:
                start_token_idx = gen_continuation.index(self.start_token)
                gen_continuation = gen_continuation[:start_token_idx]
            except ValueError:  # Generated string spans msl
                pass

            gen_continuation_str = self.tokenizer.decode(
                gen_continuation,
                skip_special_tokens=True,
            )

            # Use secondary stop seqs to cut off should-have-been-stopped content post-hoc
            for stop_word in self.until:
                if (
                    len(stop_word) > 0
                ):  # ignore '' separator, which is eos_id for some tokenizers
                    gen_continuation_str = gen_continuation_str.split(
                        stop_word
                    )[0]

            self.results.append(gen_continuation_str)
            self.sample_idx += 1


class EleutherEvalHarness(ValidationCallback):
    """
    Callback class to run EleutherAI's Evaluation Harness.
    """

    id = 0

    def __init__(
        self,
        # EEH Args
        eeh_args: Union[EleutherCLIArgs, Dict[str, Any]],
        # Cerebras specific args
        keep_data_dir: bool = False,
        every_n_vals: int = 1,
        flags: Optional[dict] = None,
        name_scope: Optional[str] = None,
        # Data Args
        batch_size: Optional[int] = None,
        data_dir: Optional[str] = None,
        max_sequence_length: Optional[int] = None,
        tokenizer_file_path: Optional[str] = None,
        eos_id: Optional[int] = None,
        **dataloader_args,
    ):
        """
        Args:
            eeh_args: `EleutherCLIArgs` dataclass or dict capturing EEH's CLI args
            keep_data_dir: Specifies whether dumped data samples should be kept for reuse.
                Defaults to False, i.e. data samples are deleted after the run.
            every_n_vals: Run the EEH script every N validations
                e.g. If the eval_frequency is set to 200 and N=2,
                     then EEH runs every 400 training steps.
                The EEH script will also always run after the final training
                iteration.
            flags: An optional dictionary of scoped global flags to set
                during the EEH run.
            name_scope: An optional string that gets added to the trainer's name scope.
            batch_size: Batch size to EleutherEvalHarness to preprocess
                input data samples from the specified eval harness tasks.
            data_dir: Path to data directory
            max_sequence_length: Maximum sequence length
            tokenizer_file_path: Path to tokenizer file
            eos_id: End of sentence token id
            dataloader_args: Any additional dataloader args, e.g. num_workers.
        """
        # Handling parsing for creating trainer from yaml
        if isinstance(eeh_args, dict):
            eeh_args = EleutherCLIArgs(**eeh_args)

        self.eh_runner = EvalHarnessRunner(eeh_args=eeh_args)

        self.dataloader_args = dict(
            batch_size=batch_size,
            data_dir=data_dir,
            keep_data_dir=keep_data_dir,
            max_sequence_length=max_sequence_length,
            tokenizer_file_path=tokenizer_file_path,
            eos_id=eos_id,
            **dataloader_args,
        )
        # Removes annoying logs relating to process forking
        appliance_environ["TOKENIZERS_PARALLELISM"] = "false"

        self.every_n_vals = every_n_vals

        self.scoped_flags = ScopedEleutherEvalHarnessFlags(**(flags or {}))

        self._id = EleutherEvalHarness.id
        EleutherEvalHarness.id += 1

        if name_scope is None:
            name_scope = f"eleuther_{self._id}"

        self.name_scope = name_scope

    @cached_property
    def has_generative_task(self):
        """Returns True if the task dictionary contains a generative task."""
        for task_obj in self.eh_runner.task_dict.items():
            if isinstance(task_obj, tuple):
                _, task_obj = task_obj
                if task_obj is None:
                    continue

            if task_obj.get_config("output_type") == "generate_until":
                return True

        return False

    @cached_property
    def has_non_generative_task(self):
        """Returns True if the task dictionary contains a non-generative task."""
        for task_obj in self.eh_runner.task_dict.items():
            if isinstance(task_obj, tuple):
                _, task_obj = task_obj
                if task_obj is None:
                    continue

            if task_obj.get_config("output_type") != "generate_until":
                return True

        return False

    def run_validation(self, trainer, loop_idx, is_last):
        if not is_last and (loop_idx + 1) % self.every_n_vals != 0:
            return

        with trainer.name_scope(self.name_scope):
            self.run(trainer)

    def run(self, trainer):
        """Run the EleutherAI Evaluation Harness.

        Args:
            trainer: the Trainer object
        """
        if not self.has_non_generative_task and not self.has_generative_task:
            raise RuntimeError(
                "Expected at least one non-generative or generative task "
                "to be present during validate runs. "
            )

        trainer.logger.info("Running EleutherAI Eval Harness")
        with self.scoped_flags:
            self.eh_runner.evaluate(
                trainer=trainer,
                model=EleutherLM(trainer, deepcopy(self.dataloader_args)),
            )


class ScopedEleutherEvalHarnessFlags(_ScopedFlags):
    """
    Class to set and restore global flags during the EleutherAI Evaluation
    Harness run.
    """

    def on_eleuther_eval_harness_start(
        self, trainer, model, val_dataloader, loop
    ):
        """Sets the global flags before the EleutherAI Evaluation Harness run."""
        self._set_all_flags()

    def on_eleuther_eval_harness_end(self, trainer, model, loop):
        """Restores the global flags after the EleutherAI Evaluation Harness run."""
        self._restore_all_flags()
