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
This module implements a generic data preprocessor called `DataPreprocessor`.
It internally uses `DataFrame` and `DataReader` to read and process data.
"""

import glob
import importlib
import json
import logging
import math
import os
import shutil
import sys
import time
import traceback
from collections import OrderedDict, defaultdict
from functools import partial
from multiprocessing import Lock, Pool, Process, Queue, Value, cpu_count
from threading import Event, Thread
from typing import Any, Dict, List, Optional, Tuple

import h5py
import numpy as np
import psutil
from datasets import load_dataset
from PIL import Image
from tqdm import tqdm

from cerebras.modelzoo.common.utils.utils import check_and_create_output_dirs
from cerebras.modelzoo.data_preparation.data_preprocessing.data_reader import (
    DataFrame,
    Reader,
    optional_lock,
)
from cerebras.modelzoo.data_preparation.data_preprocessing.pretraining_token_generator import (
    PretrainingTokenGenerator,
)
from cerebras.modelzoo.data_preparation.data_preprocessing.utils import (
    dump_args,
    get_files,
    get_size,
    update_args,
)

from cerebras.modelzoo.data_preparation.data_preprocessing.dpo_token_generator import (  # noqa
    DPOTokenGenerator,
)
from cerebras.modelzoo.data_preparation.data_preprocessing.fim_token_generator import (  # noqa
    FIMTokenGenerator,
)
from cerebras.modelzoo.data_preparation.data_preprocessing.finetuning_token_generator import (  # noqa
    FinetuningTokenGenerator,
)
from cerebras.modelzoo.data_preparation.data_preprocessing.nlg_token_generator import (  # noqa
    NLGTokenGenerator,
)
from cerebras.modelzoo.data_preparation.data_preprocessing.vsl_finetuning_token_generator import (  # noqa
    VSLFinetuningTokenGenerator,
)
from cerebras.modelzoo.data_preparation.data_preprocessing.vsl_pretraining_token_generator import (  # noqa
    VSLPretrainingTokenGenerator,
)

logging.basicConfig()
logger = logging.getLogger(__file__)
logger.setLevel(logging.INFO)


def get_available_memory():
    """
    Returns available memory in bytes.
    """
    mem = psutil.virtual_memory()
    return mem.available


def get_compression_factor(filename: str) -> int:
    """
    Calculate and return the compression factor based on a file's extension.

    Args:
        filename (str): The name of the file.

    Returns:
        int: Compression factor. Returns 3 for all compressed and parquet formats,
             otherwise returns 1 for uncompressed formats.
    """
    compressed_formats = [
        ".jsonl.zst",
        ".jsonl.zst.tar",
        ".json.gz",
        ".parquet",
    ]

    for format in compressed_formats:
        if filename.endswith(format):
            return 3  # compression factor for compressed/parquet formats

    return 1  # default factor for uncompressed formats


def format_time(seconds):
    """
    Format seconds into a human-readable string showing hours:minutes:seconds,
    minutes:seconds, or seconds.
    """
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h:{minutes:02d}m:{seconds:02d}s"
    elif minutes:
        return f"{minutes}m:{seconds:02d}s"
    else:
        return f"{seconds}s"


def update_progress(
    pbar: tqdm,
    progress_counter: Value,
    total_chunks: int,
    data_stats: dict,
    start_time: float,
    stop_event: Event,
) -> None:
    """
    Update the progress bar based on the current progress.

    Args:
        pbar (tqdm): The progress bar instance.
        progress_counter (Value): A shared counter to track progress across processes.
        total_chunks (int): Total chunks to process.
        start_time (float): The start time of the process.
        stop_event (Event): Event to signal when to stop updating progress.

    Returns:
        None
    """
    pbar.total = total_chunks
    while not stop_event.is_set():
        progress = progress_counter.value
        # Update total if actual progress exceeds estimated total_chunks
        if progress >= pbar.total:
            pbar.total = progress + 1  # Update the progress bar total

        if progress > pbar.n:
            num_processed = progress - pbar.n
            pbar.update(num_processed)
            elapsed_time = time.time() - start_time
            avg_time_per_chunk = elapsed_time / pbar.n
            estimated_remaining = avg_time_per_chunk * (pbar.total - pbar.n)
            formatted_estimated_remaining = format_time(estimated_remaining)
            # Update progress bar description with processed/total chunks
            pbar.set_description(f"Processing {pbar.n}/{pbar.total} chunks")
            # Update the progress bar postfix with avg processing time and estimated time
            postfix_items = OrderedDict(
                avg_time=f"{avg_time_per_chunk:.3f}s/chunk",
                eta=formatted_estimated_remaining,
                discarded=data_stats["discarded"].value,
                processed=data_stats["processed"].value,
            )
            pbar.set_postfix(
                postfix_items,
                refresh=True,
            )
        time.sleep(0.5)


def check_and_create_dir(dir: Optional[str], split_dir: Optional[str]) -> str:
    """
    Ensures a directory exists, optionally handling a subdirectory. It prompts
    the user for action if the directory already has files.

    Args:
    dir (Optional[str]): Base directory path. Defaults to 'input_dir' in cwd.
    split_dir (Optional[str]): Subdirectory to add to the base directory.

    Returns:
    str: The final directory path ensured to exist.
    """
    # Set default directory if none provided
    if dir is None:
        dir = os.path.join(os.getcwd(), 'input_dir')

    # Append subdirectory if provided
    if split_dir:
        dir = os.path.join(dir, split_dir)

    # Check for existing files and handle existing files
    if os.path.isdir(dir) and os.listdir(dir):
        _in = input(
            "Input directory already contains file(s). Do you want to delete "
            "the folder and download the dataset again? "
            "(yes/no): "
        )
        if _in.lower() in ["y", "yes"]:
            shutil.rmtree(dir)
            os.makedirs(dir)  # Recreate directory after removal
        elif _in.lower() in ["n", "no"]:
            return dir
        else:
            raise ValueError(
                f"Inputs can be yes, no, y, or n. Received {_in}!!"
            )
    else:
        # Create directory if it does not exist
        os.makedirs(dir, exist_ok=True)

    return dir


def default_hook(x):
    return x


def save_image_locally(example, idx, image_key, image_dir):
    image_data = example[image_key]

    if isinstance(image_data, list):
        image_paths = []
        for i, img_data in enumerate(image_data):
            if img_data is None:
                image_paths.append(None)
            else:
                image_path = os.path.join(image_dir, f"{idx}_{i}.png")
                if img_data is None:
                    image_paths.append(None)
                    continue
                if isinstance(img_data, Image.Image):
                    img_data.save(image_path)
                    image_paths.append(f"{idx}_{i}.png")
                elif isinstance(img_data, str):
                    image_paths.append(img_data)
                else:
                    raise ValueError(
                        f" Image data format - {type(image_data)} is not supported"
                    )

        example[image_key] = image_paths
    else:
        if image_data is None:
            example[image_key] = None
        else:
            image_path = os.path.join(image_dir, f"{idx}.png")
            if isinstance(image_data, Image.Image):
                image_data.save(image_path)
                example[image_key] = f"{idx}.png"
            elif isinstance(image_data, str):
                example[image_key] = image_data
            else:
                raise ValueError(
                    f" Image data format - {type(image_data)} is not supported"
                )
    return example


def update_data_stats(
    final_data_stats, data_stats: Dict[str, int], shared_stats=False
) -> None:

    if shared_stats:
        for key in data_stats:
            final_data_stats[key].value += data_stats[key]
    else:
        for key in data_stats:
            final_data_stats[key] += data_stats[key]


class DataPreprocessor:
    def __init__(self, params):
        """
        Initialize the class with given parameters.
        Args:
            params (dict): Configuration parameters.
        """
        self.params = params
        self.json_params_file = None
        self.running_avg_processing_time = 0
        self.chunks_processed = 0
        self.process_params()

    def load_dataset(self, input_data_params: Dict[str, Optional[str]]) -> str:
        """
        Loads a dataset from a specified source and saves it in a specified format
        in the given directory, potentially within a subdirectory denoted by a 'split'.

        Args:
        input_data_params (Dict[str, Optional[str]]): Parameters for dataset loading
            including 'source', 'split' (optional), and 'format'.

        Returns:
        str: The directory where the dataset has been saved.

        Raises:
        ValueError: If the specified format is not supported.
        """
        split_type = input_data_params.pop('split', None)
        cache_dir = input_data_params.pop('cache_dir', None)
        cache_dir = check_and_create_dir(cache_dir, split_type)
        source_dataset = input_data_params.pop('source')
        input_data_params = (
            {} if input_data_params is None else input_data_params
        )
        # Load the dataset with or without the split
        if split_type is not None:
            dataset = load_dataset(
                source_dataset,
                split=split_type,
                cache_dir=cache_dir,
                **input_data_params,
            )
        else:
            dataset = load_dataset(
                source_dataset, cache_dir=cache_dir, **input_data_params
            )
        if self.data_keys.get("image_key") in dataset.column_names:
            process_images_fn = partial(
                save_image_locally,
                image_key=self.data_keys.get("image_key"),
                image_dir=self.image_dir,
            )
            dataset = dataset.map(
                process_images_fn, with_indices=True, num_proc=self.processes
            )
        # Determine the file path based on format
        format_type = input_data_params.get('format', 'parquet')
        file_path = os.path.join(cache_dir, "data", f"dataset.{format_type}")
        # Save the dataset in the desired format
        if format_type == 'parquet':
            dataset.to_parquet(file_path)
        elif format_type == 'jsonl':
            dataset.to_json(file_path, orient='records', lines=True)
        else:
            ValueError(
                f"{format_type} is not supported by the data preprocessor."
            )
        logger.info(f"Dataset saved in {format_type} format at {file_path}")
        return os.path.join(cache_dir, "data")

    def process_params(self) -> None:
        """
        Process parameters by calling various initialization methods.
        """
        self.setup_output_directory()
        self.process_setup_params()
        self.process_dataset_params()
        self.process_processing_params()
        self.handle_input_files()
        self.initialize_miscellaneous_attributes()
        self.check_unused_params()

    def setup_output_directory(self) -> None:
        """
        Set up the output directory based on provided configuration.
        """
        self.output_dir = self.params["setup"].get("output_dir", "./output/")
        if not self.params["processing"].get("resume_from_checkpoint", False):
            check_and_create_output_dirs(self.output_dir, filetype="h5")
        logger.info(f"\nWriting data to {self.output_dir}.\n")
        self.json_params_file = os.path.join(
            self.output_dir, "data_params.json"
        )
        self.checkpoint_path = os.path.join(self.output_dir, "checkpoint.txt")
        dump_args(self.params, self.json_params_file)

    def handle_input_files(self) -> None:
        """
        Handle input files based on provided configuration.
        """
        setup_params = self.params["setup"]
        metadata_files = setup_params.pop("metadata_files", None)
        if metadata_files:
            metadata_files = metadata_files.split(",")
        input_data_params = setup_params.pop("data", None)
        data_source_type = input_data_params.pop("type", None)
        input_dir = None
        if data_source_type == 'huggingface':
            input_dir = self.load_dataset(input_data_params)
        elif data_source_type == 'local':
            input_dir = input_data_params.pop("source")
            assert (
                input_dir
            ), "Input data directory must be specified in source for local data"
        else:
            ValueError(
                f"Invalid data source type: {data_source_type}. Source type can be ['huggingface', 'local']"
            )
        self.input_files = sorted(
            get_files(input_dir=input_dir, metadata_files=metadata_files)
        )

    def process_setup_params(self) -> None:
        """
        Setup the number of processes based on provided configuration.
        """
        self.processes = self.params["setup"].pop("processes", cpu_count())
        self.mode = self.params["setup"].pop("mode", None)
        if self.mode is None:
            raise ValueError(
                "Data preprocessing mode is not set. Please set it."
            )
        if self.mode == 'custom':
            logger.info(f"Initializing custom processing mode")
            self.token_generator_name = self.params["setup"].get(
                "token_generator"
            )
            if not self.token_generator_name:
                raise ValueError(
                    "Token generator name is not provided for custom mode. Please provide it."
                )
            else:
                self.token_generator_name = self.token_generator_name.split(
                    ":"
                )[1]

        self.image_dir = self.params["setup"].get("image_dir")

    def check_unused_params(self) -> None:
        """
        Check for any unused parameters and log them as warnings.
        """
        unused_setup_params = [
            key for key in self.params["setup"].keys() if key != "output_dir"
        ]
        if unused_setup_params:
            logger.warning(
                f"\nThe following setup params are unused: {', '.join(unused_setup_params)}"
            )

        if self.params.get("dataset"):
            logger.warning(
                "The following dataset params are unused: "
                + ", ".join(self.params["dataset"].keys())
            )
        if self.params.get("processing"):
            logger.warning(
                "The following processing params are unused: "
                + ", ".join(self.params["processing"].keys())
            )

    def process_dataset_params(self) -> None:
        """
        Process dataset specific parameters.
        """
        dataset_params = self.params.get("dataset", {})
        use_vsl = dataset_params.get("use_vsl", False)
        self.multimodal = dataset_params.get("is_multimodal", False)
        if self.multimodal and self.image_dir:
            os.makedirs(self.image_dir, exist_ok=True)
        self.training_objective = dataset_params.get("training_objective", None)
        # Set the token generator name
        if self.mode == "pretraining":
            if self.training_objective == 'fim':
                logger.info(f"Initializing fill in the middle pretraining mode")
                self.token_generator_name = "FIMTokenGenerator"
            else:
                if use_vsl:
                    logger.info(f"Initializing VSL pretraining mode")
                else:
                    logger.info(f"Initializing pretraining mode")
                self.token_generator_name = (
                    "VSLPretrainingTokenGenerator"
                    if use_vsl
                    else "PretrainingTokenGenerator"
                )
        elif self.mode == "finetuning":
            if use_vsl:
                logger.info(f"Initializing VSL finetuning mode")
            else:
                logger.info(f"Initializing finetuning mode")
            self.token_generator_name = (
                "VSLFinetuningTokenGenerator"
                if use_vsl
                else "FinetuningTokenGenerator"
            )
        elif self.mode == "dpo":
            logger.info(f"Initializing dpo mode")
            self.token_generator_name = "DPOTokenGenerator"
        elif self.mode == "nlg":
            logger.info(f"Initializing dpo mode")
            self.token_generator_name = "NLGTokenGenerator"
        else:
            if self.mode != "custom":
                ValueError(
                    f"Invalid processor mode specified. The modes can be ['pretraining', 'finetuning', 'dpo', 'nlg', 'custom']"
                )

        ## initialize the final data statistics
        stats_fields = [
            "discarded",
            "processed",
            "successful",
            "examples",
            "total_raw_docs",
            "raw_docs_skipped",
            "loss_valid_tokens",
            "num_pad_tokens",
            "non_pad_tokens",
            "num_masked_tokens",
            "num_tokens",
            "normalized_bytes_count",
            "normalized_chars_count",
            "raw_bytes_count",
            "raw_chars_count",
            "num_sequences_before_packing",
        ]
        self.final_data_stats = {field: Value("Q", 0) for field in stats_fields}

    def estimate_queue_size(self, fraction_of_memory=0.5):
        """
        Estimates an optimal queue size based on the max_chunk_size and a fraction of available system memory.

        Args:
        - fraction_of_memory: Fraction of available system memory to be used for queues.

        Returns:
        - An integer representing the optimal queue size.
        """
        available_memory = get_available_memory()
        memory_for_queues = available_memory * fraction_of_memory
        queue_size = int(
            memory_for_queues
            / (self.read_chunk_size * (self.processes - 1) * 2)
        )
        return queue_size

    def process_processing_params(self) -> None:
        """
        Process the processing parameters and initialize relevant class attributes.
        """
        processing_params = self.params["processing"]
        self.resume_from_checkpoint = processing_params.pop(
            "resume_from_checkpoint", False
        )
        self.max_seq_length = processing_params.get("max_seq_length", 2048)
        self.read_chunk_size = (
            processing_params.pop("read_chunk_size", 1024) * 1024
        )  # By default, set to 1 MB.
        self.write_chunk_size = (
            processing_params.pop("write_chunk_size", 1024) * 1024
        )  # write_chunk_size is given in KB.
        formatted_max_chunk_size = self.human_readable_size(
            self.read_chunk_size
        )

        logger.info(f"\nChunk size : {formatted_max_chunk_size}.\n")
        self.write_in_batch = processing_params.pop("write_in_batch", False)
        self.read_hook_path = processing_params.pop("read_hook", None)
        self.read_hook_kwargs = processing_params.pop("read_hook_kwargs", None)
        if not self.read_hook_path:
            raise ValueError("Read hook path is missing.")
        if not self.read_hook_kwargs:
            raise ValueError("Read hook kwargs is missing.")
        self.data_keys = {
            key: value
            for key, value in self.read_hook_kwargs.items()
            if key.endswith('_key')
        }
        if self.data_keys == {}:
            raise ValueError(
                f"No data keys found. Please provide data keys in 'read_hook_kwargs'"
            )

        self.read_hook_fn = self.load_read_hook_fn()
        self.shuffle = processing_params.pop("shuffle", False)
        if self.shuffle:
            self.shuffle_seed = processing_params.get("shuffle_seed", 0)
            self.writer_process_num = (self.processes - 1) // 2
        else:
            self.writer_process_num = math.ceil((self.processes - 1) / 10)

        self.skip_jsonl_decoding_error = processing_params.pop(
            "UNSAFE_skip_jsonl_decoding_errors", False
        )

        self.tokenize_process_num = self.processes - 1 - self.writer_process_num
        self.initialize_tokenizer(processing_params)

        # Create tokenizer queues for each tokenizer process
        self.tokenizer_queues = None
        if self.tokenize_process_num > 0:
            # Set up communication queues
            self.fraction_of_RAM_alloted = processing_params.get(
                "fraction_of_RAM_alloted", 0.7
            )
            queue_size = min(
                self.estimate_queue_size(
                    fraction_of_memory=self.fraction_of_RAM_alloted
                ),
                50,
            )
            if queue_size == 0:
                raise ValueError(
                    """
                The max_chunk_size set at present exceeds what can be allocated in memory.
                To carry out this preprocessing task, it's necessary to reduce the max_chunk_size.
                """
                )
            self.tokenizer_queues = [
                Queue(maxsize=queue_size)
                for _ in range(self.tokenize_process_num)
            ]
            self.writer_queues = [
                Queue(maxsize=queue_size)
                for _ in range(self.tokenize_process_num)
            ]

        if isinstance(
            self.token_generator, PretrainingTokenGenerator
        ) and not isinstance(
            self.token_generator, VSLPretrainingTokenGenerator
        ):
            self.prefix_queue = Queue()

    def load_read_hook_fn(self):

        from functools import partial

        module_name, func_name = self.read_hook_path.rsplit(':', 1)
        mod = importlib.import_module(module_name)
        func = getattr(mod, func_name)

        # Use functools.partial to bind the kwargs to the function
        read_hook_fn = partial(func, **self.read_hook_kwargs)
        return read_hook_fn

    def initialize_tokenizer(self, processing_params: Dict[str, Any]) -> None:
        """
        Initialize tokenizer based on the provided `tokenizer_type` parameter.

        Args:
            processing_params (Dict[str, Any]): Dictionary of processing parameters.
        """
        hf_tokenizer = processing_params.pop("huggingface_tokenizer", None)
        custom_tokenizer = processing_params.pop("custom_tokenizer", None)

        if hf_tokenizer and custom_tokenizer:
            raise ValueError(
                f"Both custom and huggingface tokenizer cannot be provided. Please provide one tokenizer"
            )
        elif not hf_tokenizer and not custom_tokenizer:
            raise ValueError(
                f"Tokenizer is not provided. Please provide either huggingface_tokenizer or custom_tokenizer"
            )

        tokenizer_params = processing_params.pop("tokenizer_params", None)
        is_gpt2_tokenizer = False
        if hf_tokenizer:
            self.initialize_huggingfacetokenizer(hf_tokenizer, tokenizer_params)
        else:
            if custom_tokenizer == "gpt2tokenizer":
                is_gpt2_tokenizer = True
                self.initialize_gpt2tokenizer(tokenizer_params)
            elif custom_tokenizer == "neoxtokenizer":
                self.initialize_neoxtokenizer(tokenizer_params)
            else:
                logger.info(
                    "Initializing the tokenizer as a custom tokenizer..."
                )
                self.initialize_customtokenizer(
                    custom_tokenizer, tokenizer_params
                )

        # Override eos id and pad id from user args
        if (
            processing_params.get("eos_id") is not None
        ):  # important as id could be set to 0
            logger.info(
                f"Overriding the eos id {self.eos_id} from the tokenizer with supplied eos id: {processing_params['eos_id']}."
            )
            self.eos_id = processing_params.pop("eos_id")
            self.pad_id = self.eos_id
        if processing_params.get("pad_id") is not None:
            logger.info(
                f"Overriding the pad id {self.pad_id} from the tokenizer with supplied pad id: {processing_params['pad_id']}."
            )
            self.pad_id = processing_params.pop("pad_id")
            if not self.eos_id:
                self.eos_id = self.pad_id  # set pad id same as eos id
            elif self.pad_id != self.eos_id and is_gpt2_tokenizer:
                logger.info(
                    f"Pad id {self.pad_id} supplied from command line is different from eos id {self.eos_id}. For GPT2 tokenizer, pad id and eos id must be the same. Setting pad id to eos id."
                )
                self.pad_id = self.eos_id

        if self.mode == "custom":
            module_path, class_name = (
                self.params["setup"].pop("token_generator").split(":")
            )
            module = importlib.import_module(module_path)
            TokenGeneratorClass = getattr(module, class_name)
            self.token_generator = TokenGeneratorClass(
                self.params, self.tokenizer, self.eos_id, self.pad_id
            )
        else:
            if self.token_generator_name == "NLGTokenGenerator":
                self.token_generator = getattr(
                    sys.modules[__name__], self.token_generator_name
                )(self.max_seq_length)
            else:
                self.token_generator = getattr(
                    sys.modules[__name__], self.token_generator_name
                )(self.params, self.tokenizer, self.eos_id, self.pad_id)

        # Update eos_id, pad_id and features in processing section, for TokenFlow.
        updated_args = {
            "pad_id": self.pad_id,
            "eos_id": self.eos_id,
            "features": getattr(self.token_generator, 'features', []),
        }
        update_args(updated_args, self.json_params_file)

    def initialize_gpt2tokenizer(
        self, tokenizer_params: Dict[str, Any]
    ) -> None:
        """
        Initialize GPT-2 tokenizer.

        Args:
            processing_params (Dict[str, Any]): Dictionary of processing parameters.
        """
        vocab_file = tokenizer_params.pop("encoder_file", None)
        encoder_file = tokenizer_params.pop("vocab_file", None)

        if not vocab_file:
            raise ValueError(
                "`vocab_file` is missing for GPT2 tokenizer, please provide it under `tokenizer_params`."
            )
        if not encoder_file:
            raise ValueError(
                "`encoder_file` is missing for GPT2 tokenizer, please provide it using `tokenizer_params`."
            )

        tokenizer_params = {} if tokenizer_params is None else tokenizer_params
        from transformers import GPT2TokenizerFast

        self.tokenizer = GPT2TokenizerFast(
            vocab_file=vocab_file,
            merges_file=encoder_file,
            name_or_path="gpt2-tokenizer",
            **tokenizer_params,
        )

        self.eos_id = self.tokenizer.convert_tokens_to_ids("<|endoftext|>")
        self.pad_id = self.tokenizer.convert_tokens_to_ids("<|endoftext|>")

    def initialize_neoxtokenizer(
        self, tokenizer_params: Dict[str, Any]
    ) -> None:
        """
        Initialize Neox tokenizer.

        Args:
            processing_params (Dict[str, Any]): Dictionary of processing parameters.
        """
        from tokenizers import Tokenizer
        from transformers import PreTrainedTokenizerFast

        encoder_file = tokenizer_params.pop("encoder_file", None)

        if not encoder_file:
            raise ValueError(
                "`encoder_file` is missing for Neox tokenizer, please provide it using `tokenizer_params`."
            )

        tokenizer_params = {} if tokenizer_params is None else tokenizer_params
        tokenizer_model = Tokenizer.from_file(encoder_file)
        self.tokenizer = PreTrainedTokenizerFast(
            tokenizer_object=tokenizer_model,
            name_or_path="neox-tokenizer",
            **tokenizer_params,
        )

        self.eos_id = self.tokenizer.convert_tokens_to_ids("<|endoftext|>")
        self.pad_id = self.tokenizer.convert_tokens_to_ids("<|padding|>")

        if self.pad_id is None:
            self.pad_id = self.eos_id

    def initialize_huggingfacetokenizer(
        self, hf_tokenizer: str, tokenizer_params: Dict[str, Any]
    ) -> None:
        """
        Initialize Hugging Face tokenizer.

        Args:
            hf_tokenizer: str: HuggingFace tokenizer name.
            processing_params (Dict[str, Any]): Dictionary of processing parameters.
        """
        from transformers import AutoTokenizer

        tokenizer_params = {} if tokenizer_params is None else tokenizer_params
        self.tokenizer = AutoTokenizer.from_pretrained(
            pretrained_model_name_or_path=hf_tokenizer,
            **tokenizer_params,
        )
        self.eos_id = self.tokenizer.eos_token_id
        self.pad_id = (
            self.eos_id
            if self.tokenizer.pad_token_id is None
            else self.tokenizer.pad_token_id
        )

    def initialize_customtokenizer(
        self,
        custom_tokenizer,
        tokenizer_params: Dict[str, Any],
    ) -> None:
        """
            Initialize custom tokenizer.

        Args:
            custom_tokenizer: str: Path to implemenation of custom tokenizer.
            tokenizer_params: (Dict[str, Any]): Dictionary of tokenizer parameters.
        """
        tokenizer_params = {} if tokenizer_params is None else tokenizer_params

        module, class_name = custom_tokenizer.rsplit(':', 1)
        module = importlib.import_module(module)
        TokenizerClass = getattr(module, class_name)

        self.tokenizer = TokenizerClass(**tokenizer_params)
        self.eos_id = self.tokenizer.eos_token_id
        self.pad_id = self.tokenizer.pad_token_id

    def initialize_miscellaneous_attributes(self) -> None:
        """
        Initialize miscellaneous attributes.
        """
        self.n_examples = (
            0  ## stores the total number of sequences in the current dataset
        )

    def get_params_file(self) -> str:
        """
        Retrieve the path to the JSON parameters file.

        Returns:
            str: Path to the JSON parameters file.
        """
        return self.json_params_file

    def get_output_dir(self) -> str:
        """
        Retrieve the output directory path.

        Returns:
            str: Path to the output directory.
        """
        return self.output_dir

    def calculate_total_size(self) -> int:
        """
        Calculate the total size of all input files, taking compression
        factors into consideration.

        Returns:
            int: The total size of all input files in bytes.
        """
        total_size = sum(
            os.path.getsize(file) * get_compression_factor(file)
            for file in self.input_files
        )
        return total_size

    def human_readable_size(self, size, decimal_places=2):
        """
        Convert a size in bytes to a human-readable format (e.g., KB, MB, GB).

        Args:
            size (int): Size in bytes.
            decimal_places (int): Number of decimal places for rounding.

        Returns:
            str: Formatted size string.
        """
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0 or unit == "TB":
                break
            size /= 1024.0
        return f"{size:.{decimal_places}f} {unit}"

    def calculate_total_chunks(self, total_size: int) -> int:
        """
        Calculate the total number of chunks based on the given total size
        and the predefined max chunk size.

        Parameters:
            total_size (int): The total size of the data in bytes.

        Returns:
            int: Total number of chunks.
        """
        max_chunk_size_bytes = self.read_chunk_size
        return math.ceil(total_size / max_chunk_size_bytes)

    def read_checkpoint(self, num_writers) -> List[Tuple[int, int, int]]:
        """
        This function reads the checkpoint args from the created checkpoint file.
        Parameters:
            num_writers: The number of writer processes
        """

        # Collect all the stats from previous processing
        # Pattern to find checkpoint stats JSON files
        checkpoint_pattern = os.path.join(
            self.output_dir, 'checkpoint_process_stats*.json'
        )
        if self.resume_from_checkpoint:
            # Read and aggregate stats from each file
            for stats_file in glob.glob(checkpoint_pattern):
                with open(stats_file, 'r') as file:
                    stats_data = json.load(file)
                # Update final_data_stats with aggregated values
                update_data_stats(self.final_data_stats, stats_data, True)

        process_checkpoints = [
            (0, 0, 0, 0, 0) for process in range(num_writers)
        ]
        root, extension = os.path.splitext(self.checkpoint_path)

        for pid in range(num_writers):
            process_checkpoint_path = root + f'_process_{pid}.txt'
            if self.resume_from_checkpoint and os.path.isfile(
                process_checkpoint_path
            ):
                try:
                    with open(process_checkpoint_path, "r") as file:
                        (
                            file_idx,
                            doc_idx,
                            start_chunk_number,
                            num_chunks_written,
                            num_sequences_written,
                        ) = [int(i) for i in file.read().split(", ")]
                    process_checkpoints[pid] = (
                        file_idx,
                        doc_idx,
                        start_chunk_number,
                        num_chunks_written,
                        num_sequences_written,
                    )

                    logger.info(
                        f"Process {pid} resuming from file number: {file_idx}, "
                        f"and number of hdf5 files written = {num_chunks_written}"
                    )
                except Exception as e:
                    # if checkpoint path is at initialization,
                    # file may exist, but no data might be written in the file
                    # in that event, do not do anything, go to the final return
                    logger.error(e)
        return process_checkpoints

    def write_remaining_prefix(self, chunk_locks, pid) -> Tuple[int, Dict]:
        """
        This function writes the prefix remaining after processing LMData when pack_sequences is set to true.
        Parameters:
            chunk_locks : List of locks for appending to hdf5 files during shuffling
            pid: Process id of the current process

        """

        root, _ = os.path.splitext(self.checkpoint_path)
        process_checkpoint_path = root + f'_process_{pid}.txt'
        ## write remaining prefix from all processes for LMData tasks when pack sequences is set to true
        if not (
            self.token_generator_name == "PretrainingTokenGenerator"
            or self.token_generator_name == "FIMTokenGenerator"
        ):
            return
        else:
            output_file_name = os.path.join(
                self.output_dir,
                f"output_chunk_{pid}_{0}_{0}_{0}.h5",
            )

            prefix_stats = {
                "num_pad_tokens": 0,
                "non_pad_tokens": 0,
                "num_masked_tokens": 0,
                "loss_valid_tokens": 0,
                "num_tokens": 0,
            }
            if self.shuffle:
                np.random.seed(self.shuffle_seed)
            prefix = []
            sentinels_received = 0
            try:
                while True:
                    curr_prefix = self.prefix_queue.get()
                    if curr_prefix is None:
                        sentinels_received += 1
                        if sentinels_received == self.tokenize_process_num:
                            break
                    else:
                        if isinstance(curr_prefix, list):
                            prefix.extend(curr_prefix)
                        elif isinstance(curr_prefix, dict):
                            prefix.append(curr_prefix)

                if prefix != []:
                    (
                        encoded_prefix,
                        prefix_stats,
                    ) = self.token_generator.encode_leftover_prefix(prefix)
                    if encoded_prefix != {}:
                        base_name, extension = os.path.splitext(
                            output_file_name
                        )
                        chunk_number = int(base_name.split('_')[-1])
                        df_chunk = DataFrame(self.data_keys)
                        for key, value in encoded_prefix.items():
                            df_chunk.tokenized_data[key].append(value)
                        update_data_stats(
                            self.final_data_stats, prefix_stats, True
                        )
                        chunk_data = chunk_number, df_chunk
                        if not self.shuffle:
                            buffer = {}
                            for (
                                data_label,
                                data,
                            ) in df_chunk.tokenized_data.items():
                                data = np.concatenate(data, axis=0)
                                buffer.setdefault(data_label, []).append(data)

                            with h5py.File(output_file_name, "w") as h5f:
                                self.save_buffer_to_hdf5(
                                    h5f, buffer, self.write_in_batch
                                )
                                self.final_data_stats["examples"].value += int(
                                    h5f.attrs["n_examples"]
                                )
                        else:
                            n_examples = self.append_df_to_hdf5(
                                df_chunk,
                                self.output_dir,
                                chunk_locks,
                            )

                            self.final_data_stats[
                                "examples"
                            ].value += n_examples
                        df_chunk.tokenized_data.clear()
            except Exception as e:
                logger.error(
                    f"Exception in write_remaining_prefix: \n {traceback.format_exc()}",
                )

    @staticmethod
    def shuffle_single_file(args):

        file_path, shuffle_seed, pid = args
        np.random.seed(shuffle_seed + pid)
        with h5py.File(file_path, 'r+') as hf:
            dataset_length = hf[list(hf.keys())[0]].shape[0]
            # Generate a consistent shuffle index
            shuffle_index = np.arange(dataset_length)
            np.random.shuffle(shuffle_index)
            # Apply the shuffle index to each dataset
            for key in hf.keys():
                dataset = hf[key]
                data_array = dataset[:]
                dataset[...] = data_array[shuffle_index]

    def split_shuffle_second_pass(self):
        """
        This function divides the output hdf5 files into different processes and prepares them for the
        second pass of shuffling.
        """
        logger.info("The second pass of shuffling has started")

        # Get the list of HDF5 files
        hdf5_file_list = sorted(
            glob.glob(os.path.join(self.output_dir, "*.h5"))
        )
        hdf5_file_list_length = len(hdf5_file_list)

        # Check if there are no HDF5 files present
        if hdf5_file_list_length == 0:
            logger.error(
                "No HDF5 files found in the output directory for shuffling."
            )
            return

        try:
            # Process the first file to estimate the available memory
            first_file_path = hdf5_file_list[0]
            with h5py.File(first_file_path, 'r') as hf:
                dataset = hf["data"]
                available_memory = psutil.virtual_memory().available
                dataset_size = dataset.dtype.itemsize * np.prod(dataset.shape)
                estimate_second_phase_processes = math.ceil(
                    (0.4 * available_memory) / dataset_size
                )
            # Calculate the number of processes to use in the second phase
            second_phase_processes = min(
                estimate_second_phase_processes, self.processes
            )
            args_list = [
                (file_path, self.shuffle_seed, pid)
                for pid, file_path in enumerate(hdf5_file_list)
            ]

            # Start processing with a pool of processes
            with Pool(processes=second_phase_processes) as pool:
                with tqdm(
                    total=hdf5_file_list_length,
                    desc="Processing",
                    dynamic_ncols=True,
                ) as pbar:
                    for _ in pool.imap_unordered(
                        DataPreprocessor.shuffle_single_file, args_list
                    ):
                        pbar.update()

        except Exception as e:
            logger.error(
                f"Exception in split_shuffle_second_pass: \n {traceback.format_exc()}",
            )

    def process_files(
        self,
        file_paths,
        process_idx,
        checkpoint_args,
        progress_counter,
        chunk_locks,
    ) -> None:
        """
        Process the given files, tokenize the data chunks, and save to HDF5 format.

        Parameters:
            file_paths: list of file_paths.
            process_idx: Index of current process among all process spawned for file split
            checkpoint_args (Tuple[int, int, int]): File index, doc start index, and hdf5 index.
            progress_counter (Value[int]): Shared counter tracking number of processed chunks.
            chunk_locks : List of locks for appending to hdf5 files during shuffling

        """
        try:
            if self.shuffle:
                np.random.seed(self.shuffle_seed + process_idx)

            # Initial setup
            reader = Reader(
                file_paths,
                max_chunk_size=self.read_chunk_size,
                keys=self.data_keys,
                read_hook_fn=self.read_hook_fn,
                skip_jsonl_decoding_error=self.skip_jsonl_decoding_error,
            )

            (
                file_idx,
                doc_start_idx,
                start_chunk_number,
                num_chunks_written,
                num_sequences_written,
            ) = checkpoint_args
            process_chunk_number = start_chunk_number
            checkpoint_args = (file_idx, doc_start_idx)
            root, extension = os.path.splitext(self.checkpoint_path)
            process_checkpoint_path = root + f'_process_{process_idx}.txt'
            process_stats_path = root + f'_process_stats_{process_idx}.json'

            buffer = {}
            cum_size = 0
            process_data_stats = defaultdict(int)
            for df_chunk in reader.stream_data(checkpoint_args):
                # Tokenize chunk
                df_chunk.tokenize(self.token_generator)
                update_data_stats(
                    self.final_data_stats, df_chunk.data_stats, True
                )
                update_data_stats(
                    process_data_stats, df_chunk.data_stats, False
                )
                if df_chunk.tokenized_data == {}:
                    process_chunk_number += 1
                    progress_counter.value += 1
                    continue

                checkpoint_doc_idx = df_chunk.end_doc_idx + 1
                if isinstance(
                    self.token_generator,
                    (VSLPretrainingTokenGenerator, VSLFinetuningTokenGenerator),
                ):
                    checkpoint_doc_idx = df_chunk.start_doc_idx

                if not self.shuffle:
                    for data_label, data in df_chunk.tokenized_data.items():
                        data = np.concatenate(data, axis=0)
                        if data_label not in buffer:
                            buffer[data_label] = []
                        buffer[data_label].append(data)
                    if get_size(buffer) >= self.write_chunk_size:
                        output_file_name = os.path.join(
                            self.output_dir,
                            f"output_chunk_{process_idx}_{df_chunk.file_idx}_{df_chunk.start_doc_idx}_{process_chunk_number}.h5",
                        )
                        with h5py.File(output_file_name, "w") as h5f:
                            self.save_buffer_to_hdf5(
                                h5f, buffer, self.write_in_batch
                            )
                            self.final_data_stats["examples"].value += int(
                                h5f.attrs["n_examples"]
                            )
                            process_data_stats["examples"] += int(
                                h5f.attrs["n_examples"]
                            )
                        num_chunks_written += 1
                        buffer = {}
                else:
                    n_examples = self.append_df_to_hdf5(
                        df_chunk,
                        self.output_dir,
                        chunk_locks,
                    )
                    self.final_data_stats["examples"].value += n_examples
                    process_data_stats["examples"] += n_examples
                    cum_size += get_size(df_chunk.tokenized_data)
                    if cum_size >= self.write_chunk_size:
                        num_chunks_written += 1
                        cum_size = 0
                df_chunk.tokenized_data.clear()

                progress_counter.value += 1
                process_chunk_number += 1
                checkpoint_data = [
                    df_chunk.file_idx,
                    checkpoint_doc_idx,
                    process_chunk_number,
                    num_chunks_written,
                    0,
                ]
                self.update_checkpoint(process_checkpoint_path, checkpoint_data)
            if len(buffer) > 0:
                output_file_name = os.path.join(
                    self.output_dir,
                    f"output_chunk_{process_idx}_{df_chunk.file_idx}_{df_chunk.start_doc_idx}_{process_chunk_number}.h5",
                )
                with h5py.File(output_file_name, "w") as h5f:
                    self.save_buffer_to_hdf5(h5f, buffer, self.write_in_batch)
                    self.final_data_stats["examples"].value += int(
                        h5f.attrs["n_examples"]
                    )
                    process_data_stats["examples"] += int(
                        h5f.attrs["n_examples"]
                    )
                num_chunks_written += 1
                checkpoint_data = [
                    df_chunk.file_idx,
                    checkpoint_doc_idx,
                    process_chunk_number,
                    num_chunks_written,
                    0,
                ]
                self.update_checkpoint(process_checkpoint_path, checkpoint_data)

            dump_args(process_data_stats, process_stats_path)

            if isinstance(
                self.token_generator, PretrainingTokenGenerator
            ) and not isinstance(
                self.token_generator, VSLPretrainingTokenGenerator
            ):
                if self.token_generator.prefix != []:
                    self.prefix_queue.put(self.token_generator.prefix)
                elif self.token_generator.prefix_doc != None:
                    self.prefix_queue.put(self.token_generator.prefix_doc)
                self.prefix_queue.put(None)
        except Exception as e:
            logger.error(
                f"Exception in process_files: \n {traceback.format_exc()}",
            )

    def file_split_process_dataset(self) -> None:
        """
        Process the dataset by splitting files across multiple processes.
        """
        start_time = time.time()
        self.tokenize_process_num = self.processes
        total_size = self.calculate_total_size()
        self.total_chunks = self.calculate_total_chunks(total_size)
        self.total_output_files = math.ceil(total_size / self.write_chunk_size)

        # Distribute file paths among the processes
        process_file_lists = [[] for _ in range(self.processes)]
        process_checkpoints = self.read_checkpoint(self.processes)
        num_chunks_written = sum(
            [checkpoint_args[-2] for checkpoint_args in process_checkpoints]
        )

        # Assign files to each process
        for idx, file in enumerate(self.input_files):
            target_process = idx % self.processes
            process_file_lists[target_process].append(file)

        # Setup the shared progress counter
        progress_counter = Value("i", num_chunks_written)
        if self.shuffle and self.processes > 1:
            lock_pool_size = cpu_count()
            chunk_locks = [Lock() for _ in range(lock_pool_size)]
        else:
            chunk_locks = None

        # Spawn only self.processes - 1 subprocesses, as the main process will also handle files
        processes = [
            Process(
                target=self.process_files,
                args=(
                    files,
                    pid + 1,  # Process index starts at 1 for subprocesses
                    process_checkpoints[pid + 1],  # Start at next checkpoint
                    progress_counter,
                    chunk_locks,
                ),
            )
            for pid, files in enumerate(
                process_file_lists[1:]
            )  # Exclude main process
        ]

        # Start the subprocesses
        for p in processes:
            p.start()

        # Using tqdm for progress bar
        with tqdm(
            total=self.total_chunks, desc="Processing", dynamic_ncols=True
        ) as pbar:
            stop_event = Event()  # Signal to stop progress update
            progress_thread = Thread(
                target=update_progress,
                args=(
                    pbar,
                    progress_counter,
                    self.total_chunks,
                    self.final_data_stats,
                    start_time,
                    stop_event,
                ),
            )
            progress_thread.start()

            # Main process handles the files assigned to process 0
            self.process_files(
                process_file_lists[0],
                0,  # Main process index is 0
                process_checkpoints[0],
                progress_counter,
                chunk_locks,
            )

            self.write_remaining_prefix(chunk_locks, self.processes)
            # Wait for all processes to finish
            for p in processes:
                # TODO: We had to add a timeout here
                # as a workaround to avoid hanging at the
                # join. We need to figure out a better
                # solution.
                p.join(timeout=1e-6)

            # Final update of the progress bar to make sure it reaches `progress_counter.value`
            pbar.n = progress_counter.value
            pbar.total = progress_counter.value
            pbar.refresh()
            stop_event.set()  # Signal the progress update thread to stop
            progress_thread.join()  # Wait for the progress update thread to finish
        if self.shuffle:
            self.split_shuffle_second_pass()

        elapsed_time = time.time() - start_time
        logger.info(
            f"The process_dataset function took {elapsed_time:.2f} seconds to complete."
        )

    def reader_process(self, process_checkpoints: List[Tuple]) -> None:
        """
        Reads data from input files and distributes them to the tokenizer queues.

        Args:
            process_checkpoints (List[Tuple[int, int, int, int, int]]): List of File index, doc start index, start_chunk_number,
                                                                        num_chunks_written, num_sequences_written
        """
        try:
            # Initialize reader with necessary parameters
            reader = Reader(
                self.input_files,
                max_chunk_size=self.read_chunk_size,
                keys=self.data_keys,
                read_hook_fn=self.read_hook_fn,
                skip_jsonl_decoding_error=self.skip_jsonl_decoding_error,
            )
            sorted_process_checkpoints = sorted(
                process_checkpoints, key=lambda x: x[2]
            )
            (
                file_idx,
                doc_start_idx,
                start_chunk_number,
                num_chunks_written,
                num_sequences_written,
            ) = sorted_process_checkpoints[0]
            checkpoint_args = (file_idx, doc_start_idx)
            chunk_number = start_chunk_number  # Initialize chunk number counter

            for df_chunk in reader.stream_data(checkpoint_args):
                tokenizer_index = (
                    chunk_number - start_chunk_number
                ) % self.tokenize_process_num
                writer_index = tokenizer_index % self.writer_process_num

                # Skip chunks that have already been processed
                if chunk_number < process_checkpoints[writer_index][2]:
                    chunk_number += 1
                    continue

                # Distribute chunks to tokenizer queues in a round-robin fashion
                tokenizer_queue = self.tokenizer_queues[tokenizer_index]
                tokenizer_queue.put(
                    (chunk_number, df_chunk)
                )  # Send chunk number with df_chunk
                chunk_number += 1

        except Exception as e:
            # Log error during initialization or reading
            logger.error(
                f"Exception in reader process: \n {traceback.format_exc()}",
            )

        finally:
            # Ensure that sentinel values are placed in each tokenizer queue to indicate end of reading
            for tq in self.tokenizer_queues:
                tq.put(None)

    def tokenizer_process(self, idx: int) -> None:
        """
        Tokenizes data and forwards the tokenized data to the writer queue.

        Args:
            idx (int): Queue ID to forward tokenized chunks of data.
        """
        try:
            while True:
                chunk_data = self.tokenizer_queues[idx].get()
                if chunk_data is None:  # Sentinel value indicates termination
                    if isinstance(
                        self.token_generator, PretrainingTokenGenerator
                    ) and not isinstance(
                        self.token_generator, VSLPretrainingTokenGenerator
                    ):
                        if self.token_generator.prefix != []:
                            self.prefix_queue.put(self.token_generator.prefix)
                        elif self.token_generator.prefix_doc != None:
                            self.prefix_queue.put(
                                self.token_generator.prefix_doc
                            )
                        self.prefix_queue.put(None)
                    self.writer_queues[idx].put(None)
                    break
                (
                    chunk_number,
                    df_chunk,
                ) = chunk_data  # Unpack chunk number and data frame chunk

                df_chunk.tokenize(self.token_generator)
                self.writer_queues[idx].put((chunk_number, df_chunk))
        except Exception as e:
            # Capture and log the full traceback for debugging
            logger.error(
                f"Exception in tokenizer process: {os.getpid()} \n {traceback.format_exc()}",
            )
            self.writer_queues[idx].put(None)  # Signal termination to writer
            # Signal termination to prefix queue
            if isinstance(
                self.token_generator, PretrainingTokenGenerator
            ) and not isinstance(
                self.token_generator, VSLPretrainingTokenGenerator
            ):
                self.prefix_queue.put(None)

    def writer_process(
        self,
        progress_counter: "Value[int]",
        num_sentinels: int,
        writer_idx: int,
        chunk_locks: List[Lock],
        process_checkpoints: Tuple,
    ) -> None:
        """
        Process that writes tokenized data to HDF5 format.

        Args:
            progress_counter (Value[int]): Shared counter tracking number of processed chunks.
            num_sentinels : Number of sentinels to be received for the current writer process
            writer_idx : The index of the current writer process
            chunk_locks : List of locks for appending to hdf5 files during shuffling
            process_checkpoints: Checkpoint for the current process. This is used for resuming from checkpoint.
        """

        process_data_stats = defaultdict(int)
        sentinels_received = 0
        tokenizer_idx = writer_idx
        num_chunks_written = process_checkpoints[-2]
        root, extension = os.path.splitext(self.checkpoint_path)
        process_checkpoint_path = root + f'_process_{writer_idx}.txt'
        process_stats_path = root + f'_process_stats_{writer_idx}.json'
        if self.shuffle:
            np.random.seed(self.shuffle_seed + writer_idx)

        buffer = {}
        cum_size = 0
        try:
            while True:
                chunk_data = self.writer_queues[tokenizer_idx].get()
                ## We need to allocate the writer queues to writer processes in a round robin fashion.
                ## When the writer queue index (aka tokenizer_idx) goes beyond the total writer queues(aka self.tokenize_process_num) reset it to the current writer process index(aka writer_idx).
                tokenizer_idx = tokenizer_idx + self.writer_process_num
                if tokenizer_idx >= self.tokenize_process_num:
                    tokenizer_idx = writer_idx
                if chunk_data is None:
                    sentinels_received += 1
                    if sentinels_received == num_sentinels:
                        break
                    continue
                chunk_number, df_chunk = chunk_data
                update_data_stats(
                    self.final_data_stats, df_chunk.data_stats, True
                )
                update_data_stats(
                    process_data_stats, df_chunk.data_stats, False
                )

                if df_chunk.tokenized_data == {}:
                    progress_counter.value += 1
                    continue
                else:
                    checkpoint_doc_idx = df_chunk.end_doc_idx + 1
                    if isinstance(
                        self.token_generator,
                        (
                            VSLPretrainingTokenGenerator,
                            VSLFinetuningTokenGenerator,
                        ),
                    ):
                        checkpoint_doc_idx = df_chunk.start_doc_idx
                    if not self.shuffle:
                        for data_label, data in df_chunk.tokenized_data.items():
                            data = np.concatenate(data, axis=0)
                            if data_label not in buffer:
                                buffer[data_label] = []
                            buffer[data_label].append(data)
                        if get_size(buffer) >= self.write_chunk_size:
                            output_file_name = os.path.join(
                                self.output_dir,
                                f"output_chunk_{writer_idx}_{df_chunk.file_idx}_{df_chunk.start_doc_idx}_{chunk_number}.h5",
                            )

                            with h5py.File(output_file_name, "w") as h5f:
                                self.save_buffer_to_hdf5(
                                    h5f, buffer, self.write_in_batch
                                )
                                self.final_data_stats["examples"].value += int(
                                    h5f.attrs["n_examples"]
                                )
                                process_data_stats["examples"] += int(
                                    h5f.attrs["n_examples"]
                                )
                            num_chunks_written += 1
                            buffer = {}
                    else:
                        n_examples = self.append_df_to_hdf5(
                            df_chunk,
                            self.output_dir,
                            chunk_locks,
                        )
                        self.final_data_stats["examples"].value += n_examples
                        process_data_stats["examples"] += n_examples
                        cum_size += get_size(df_chunk.tokenized_data)
                        if cum_size >= self.write_chunk_size:
                            num_chunks_written += 1
                            cum_size = 0
                    df_chunk.tokenized_data.clear()
                    progress_counter.value += 1
                    checkpoint_data = [
                        df_chunk.file_idx,
                        checkpoint_doc_idx,
                        chunk_number + 1,
                        num_chunks_written,
                        0,
                    ]
                    self.update_checkpoint(
                        process_checkpoint_path, checkpoint_data
                    )

            if len(buffer) > 0:
                output_file_name = os.path.join(
                    self.output_dir,
                    f"output_chunk_remaining_{df_chunk.file_idx}_{df_chunk.start_doc_idx}.h5",
                )
                with h5py.File(output_file_name, "w") as h5f:
                    self.save_buffer_to_hdf5(h5f, buffer, self.write_in_batch)
                    self.final_data_stats["examples"].value += int(
                        h5f.attrs["n_examples"]
                    )
                    process_data_stats["examples"] += int(
                        h5f.attrs["n_examples"]
                    )
                num_chunks_written += 1
                checkpoint_data = [
                    df_chunk.file_idx,
                    checkpoint_doc_idx,
                    chunk_number + 1,
                    num_chunks_written,
                    0,
                ]
                self.update_checkpoint(process_checkpoint_path, checkpoint_data)

            dump_args(process_data_stats, process_stats_path)
        except Exception as e:
            logger.error(
                f"Exception in writer process {os.getpid()}: \n {traceback.format_exc()}",
            )

    def task_split_process_dataset(self) -> None:
        """
        Split the dataset processing tasks across multiple processes.
        """
        start_time = time.time()
        total_size = self.calculate_total_size()
        readable_size = self.human_readable_size(total_size)
        logger.info(f"Total size of dataset: {readable_size}")
        self.total_chunks = self.calculate_total_chunks(total_size)
        self.total_output_files = math.ceil(total_size / self.write_chunk_size)
        logger.info(
            f"Approximate number of chunks to process: {self.total_chunks}"
        )
        process_checkpoints = self.read_checkpoint(self.writer_process_num)
        total_num_chunks_written = sum(
            [checkpoint_args[-2] for checkpoint_args in process_checkpoints]
        )
        progress_counter = Value("i", total_num_chunks_written)
        # Log process information
        logger.info(f"Total processes: {self.processes}")
        logger.info(f"Reader processes: 1")
        logger.info(f"Tokenizer processes: {self.tokenize_process_num}")
        logger.info(f"Writer processes: {self.writer_process_num}")

        if self.shuffle and self.writer_process_num > 1:
            lock_pool_size = 128
            chunk_locks = [Lock() for _ in range(lock_pool_size)]
        else:
            chunk_locks = None
        chunks_per_writer = [
            (
                (self.tokenize_process_num // self.writer_process_num + 1)
                if i < self.tokenize_process_num % self.writer_process_num
                else (self.tokenize_process_num // self.writer_process_num)
            )
            for i in range(self.writer_process_num)
        ]
        # Initialize and start processes
        tokenizers = [
            Process(
                target=self.tokenizer_process,
                args=(idx,),
            )
            for idx in range(self.tokenize_process_num)
        ]
        writers = []
        for idx in range(self.writer_process_num):
            writers.append(
                Process(
                    target=self.writer_process,
                    args=(
                        progress_counter,
                        chunks_per_writer[idx],
                        idx,
                        chunk_locks,
                        process_checkpoints[idx],
                    ),
                )
            )
        for t in tokenizers:
            t.start()
        for w in writers:
            w.start()

        # Use tqdm for the progress bar
        with tqdm(
            total=self.total_chunks, desc="Processing", dynamic_ncols=True
        ) as pbar:
            stop_event = Event()
            progress_thread = Thread(
                target=update_progress,
                args=(
                    pbar,
                    progress_counter,
                    self.total_chunks,
                    self.final_data_stats,
                    start_time,
                    stop_event,
                ),
            )
            progress_thread.start()
            self.reader_process(process_checkpoints)
            for t in tokenizers:
                # TODO: We had to add a timeout here
                # as a workaround to avoid hanging at the
                # join. We need to figure out a better
                # solution.
                t.join(timeout=1e-6)
            self.write_remaining_prefix(chunk_locks, self.writer_process_num)
            for w in writers:
                w.join()

            # Final update of the progress bar to make sure it reaches `progress_counter.value`
            pbar.n = progress_counter.value
            pbar.total = progress_counter.value
            pbar.refresh()
            stop_event.set()
            progress_thread.join()

        if self.shuffle:
            self.split_shuffle_second_pass()

        end_time = time.time()
        elapsed_time = end_time - start_time
        logger.info(
            f"The process_dataset function took {format_time(elapsed_time)} to complete."
        )

    def process_dataset(self) -> dict:
        """
        Process the dataset either through file split or task split methods.
        """

        if self.processes < 3:
            self.file_split_process_dataset()
        else:
            self.task_split_process_dataset()

        data_stats = defaultdict(int)
        data_stats["examples"] = 0
        ## Some statistics are present only for a subset of the tasks. Only include them if not 0
        optional_keys = [
            "num_sequences_before_packing",
            "total_raw_docs",
            "raw_docs_skipped",
        ]
        for key in self.final_data_stats:
            if key in optional_keys and self.final_data_stats[key].value == 0:
                continue
            data_stats[key] = self.final_data_stats[key].value

        if data_stats["examples"] > 0:
            data_stats["average_chars_per_sequence"] = math.ceil(
                data_stats["raw_chars_count"] / data_stats["examples"]
            )
            data_stats["average_bytes_per_sequence"] = math.ceil(
                data_stats["raw_bytes_count"] / data_stats["examples"]
            )
            if data_stats.get("num_sequences_before_packing") is not None:
                data_stats["vsl_packing_factor"] = (
                    data_stats.pop("num_sequences_before_packing")
                    / data_stats["examples"]
                )
        else:
            data_stats["average_chars_per_sequence"] = data_stats[
                "average_bytes_per_sequence"
            ] = 0

        data_stats["features"] = getattr(self.token_generator, 'features', [])
        return data_stats

    def get_vocab_size(self):
        """Get tokenizer vocabulary size
        Returns:
            vocab_size (int): text to tokenize
        """
        return len(self.tokenizer)

    def save_buffer_to_hdf5(
        self, h5file, buffer, write_in_batch, dtype="i4", compression="gzip"
    ):
        n_examples = 0
        for data_label in [*buffer]:
            data = np.concatenate(buffer[data_label], axis=0)
            if data.dtype.kind == 'S':
                dtype = h5py.string_dtype(encoding='utf-8')
            elif data.dtype == np.bool_:
                dtype = np.bool_
            else:
                dtype = "i4"
            if len(data.shape) > 1:
                chunks_shape = (
                    1,
                    *data.shape[1:],
                )  # Set chunk shape for multidimensional data
            else:
                chunks_shape = None
            n_examples = data.shape[0]
            dset = h5file.create_dataset(
                data_label,
                data=data,
                dtype=dtype,
                chunks=chunks_shape,
                compression=compression,
            )

            if not write_in_batch:
                for idx, f in enumerate(data):
                    dset[idx] = f

        h5file.attrs["n_examples"] = n_examples

    def append_df_to_hdf5(
        self, df_chunk, output_dir, chunk_locks, dtype="i4", compression="gzip"
    ):
        """
        Appends each sequence in a dataframe to different HDF5 files efficiently.
        Assumes that all data labels have the same number of entries.
        """

        # Step 1: Concatenate data for each data_label
        data_dict = {}
        n_examples = None  # Will determine after concatenation
        for data_label, data_list in df_chunk.tokenized_data.items():
            # Perform necessary concatenation along axis=0
            data = np.concatenate(data_list, axis=0)
            data_dict[data_label] = data
            if n_examples is None:
                n_examples = data.shape[0]
            else:
                assert (
                    n_examples == data.shape[0]
                ), "All data_labels must have the same number of examples"

        # Step 2: Generate shuffled indices
        shuffled_indices = np.random.choice(
            np.arange(self.total_output_files), n_examples
        )

        # Step 3: Group indices per output file
        idx_seq_to_indices = defaultdict(list)
        for idx, idx_seq in enumerate(shuffled_indices):
            idx_seq_to_indices[idx_seq].append(idx)

        # Step 4: Write data to HDF5 files in batches
        for idx_seq, indices in idx_seq_to_indices.items():
            indices = np.array(indices)
            output_file_name = os.path.join(
                output_dir, f"output_chunk_{idx_seq}.h5"
            )
            lock = (
                chunk_locks[idx_seq % len(chunk_locks)] if chunk_locks else None
            )

            with optional_lock(lock):
                with h5py.File(output_file_name, "a") as h5f:
                    # Initialize or update n_examples attribute
                    if 'n_examples' in h5f.attrs:
                        old_n_examples = h5f.attrs['n_examples']
                    else:
                        old_n_examples = 0

                    new_n_examples = old_n_examples + len(indices)
                    h5f.attrs['n_examples'] = new_n_examples

                    for data_label, data in data_dict.items():
                        # Extract elements corresponding to current indices
                        elements = data[indices]

                        # Determine appropriate dtype
                        data_dtype = elements.dtype
                        if data_dtype.kind == 'S':
                            dtype = h5py.string_dtype(encoding='utf-8')
                        elif data_dtype == np.bool_:
                            dtype = np.bool_
                        else:
                            dtype = "i4"

                        # Set chunk shape and max shape
                        if elements.ndim > 1:
                            chunks_shape = (1,) + elements.shape[1:]
                            maxshape = (None,) + elements.shape[1:]
                        else:
                            chunks_shape = None
                            maxshape = (None,)

                        if data_label not in h5f:
                            # Create dataset with maxshape for future resizing
                            h5f.create_dataset(
                                data_label,
                                data=elements,
                                dtype=dtype,
                                chunks=chunks_shape,
                                maxshape=maxshape,
                                compression=compression,
                            )
                        else:
                            # Resize dataset and append new data
                            old_size = h5f[data_label].shape[0]
                            new_size = old_size + elements.shape[0]
                            # Ensure correct shape during resizing
                            new_shape = (new_size,) + h5f[data_label].shape[1:]
                            h5f[data_label].resize(new_shape)
                            h5f[data_label][old_size:] = elements

        return n_examples

    def update_checkpoint(
        self,
        process_checkpoint_path,
        checkpoint_data,
    ):
        with open(process_checkpoint_path, "w") as file:
            file.write(
                f"{checkpoint_data[0]}, {checkpoint_data[1]}, {checkpoint_data[2]}, {checkpoint_data[3]}, {checkpoint_data[4]}"
            )
            file.flush()
