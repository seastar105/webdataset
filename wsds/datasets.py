import dataclasses
import os
import random
import warnings
from functools import partial
from itertools import islice
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import yaml

from webdataset import autodecode, cache, filters, shardlists, tariterators

try:
    from torch.utils.data import IterableDataset
except ImportError:

    class IterableDataset:
        pass


def run_pipeline(pipeline):
    """Run a list of iterators as a pipeline.

    This can be so much shorter than the Pipeline class
    because for these classes, the pipelines are fixed
    and only created once inside the constructor. Users
    never use it.

    Args:
        pipeline (list): List of callables returning iterators.
    """
    source = pipeline[0]()
    for filter in pipeline[1:]:
        assert source is not None
        source = filter(source)
        assert source is not None, filter
    for sample in source:
        yield sample


def set_pipeline_epochs(pipeline, epoch):
    """Set the epoch for all stages in the pipeline.

    For any stage that has a set_epoch method, call it with the epoch number.

    Args:
        pipeline (list): List of callables.
        epoch (int): Epoch number.
    """
    for stage in pipeline:
        if hasattr(stage, "set_epoch"):
            stage.set_epoch(epoch)


def apply_transformations(transformations, x):
    """Apply a list of transformations to a sample.

    Args:
        transformations (list): List of callables.
        x (dict): Sample.
    """
    if transformations is None or transformations == []:
        return x
    if callable(transformations):
        return transformations(x)
    if isinstance(transformations, list):
        for transformation in transformations:
            assert callable(transformation), transformation
            x = transformation(x)
        return x
    raise ValueError(f"bad transformations: {transformations}")


def fix_dots(sample):
    for k in list(sample.keys()):
        if k.startswith("__") or k.startswith("."):
            continue
        sample["." + k] = sample[k]
        del sample[k]


def map_stream(source, f=None):
    for x in source:
        yield f(x)


def map_expand(source, f=None):
    for x in source:
        y = f(x)
        if isinstance(y, Iterator):
            yield from y
        elif isinstance(y, (dict, tuple)):
            yield y
        else:
            raise ValueError(f"function {f} returned unexpected type {type(y)}")


def default_handler(exn):
    raise exn


@dataclasses.dataclass
class DatasetSpec:
    # basic dataset info
    shards: List[str] = dataclasses.field(default_factory=list)
    transformations: List[Callable] = dataclasses.field(default_factory=list)
    handler: Callable = default_handler
    check_empty: bool = False
    file_fn: Optional[Callable] = None
    repeats: int = 1
    force_size: Optional[int] = None

    # shard splitting
    shard_split_fn: Optional[callable] = None

    # shuffle options
    shuffle_size: int = -1
    shard_shuffle_size: int = 10000

    # batching options
    batch_size: Optional[int] = None
    batch_partial: bool = True
    collation_fn: Optional[Callable] = filters.default_collation_fn

    # JSON specification files
    dataset_name: Optional[str] = None
    base_url: Optional[Any] = None
    override_options: Optional[Dict[str, Any]] = None

    # caching related options
    cache_size: int = int(1e12)
    cache_dir: Optional[str] = None
    localname_fn: Optional[Callable] = None
    lru_size: int = 10
    keep_downloaded: bool = False

    # debugging
    log_shards: Optional[str] = None
    log_keys: Optional[str] = None


def add_len_method(obj):
    """Add a fake __len__ method to an object.

    This is useful for frameworks that happen to work with
    IterableDataset but still use __len__ to determine the
    length of the dataset. This is not usually recommended
    because PyTorch does not expect __len__ on IterableDataset.
    """

    def fake_len(self):
        return self.total_size

    obj.__len__ = fake_len


class SequentialDataset(IterableDataset):
    def __init__(
        self,
        *,
        args=None,
        **kw,
    ):
        self.args = DatasetSpec(args) if args is not None else DatasetSpec()
        self.args = dataclasses.replace(self.args, **kw)
        self.total_size = -1
        self.epoch = -1
        self.shardlist = None
        self.log_shards_stream = None
        self.log_keys_stream = None
        self.init_pipeline(args)
        if self.args.cache_dir is not None:
            self.cache = cache.FileCache(
                cache_dir=cache_dir,
                cache_size=cache_size,
                handler=args.handler,
            )
        else:
            self.cache = None
        self.read_shardlist()

    def open_log(self, dest):
        if dest is None:
            stream = None
        elif dest == "-":
            stream = sys.stderr
        elif isinstance(dest, str):
            stream = open(dest, "w")
        else:
            stream = dest
        return stream

    def log_shards(self, source):
        if self.log_shards_stream is None:
            self.log_shards_stream = self.open_log(self.args.log_shards)
        for x in source:
            if self.log_shards_stream is not None:
                self.log_shards_stream.write(x["__url__"])
            yield x

    def log_keys(self, source):
        if self.log_keys_stream is None:
            self.log_keys_stream = self.open_log(self.args.log_keys)
        for x in source:
            if self.log_keys_stream is not None:
                self.log_keys_stream.write(x["__key__"])
            yield x

    def init_pipeline(self, args):
        self.pipeline = [
            self.iterate_shards,
            self.split_shards,
            self.repeat_shards,
            self.shuffle_shards,
            self.open_shards,
            self.log_shards,
            self.iterate_tar_files,
            self.rename_files,
            self.group_by_keys,
            self.log_keys,
            self.shuffle_samples,
            self.transform_samples,
            self.limit_size,
            self.batch_samples,
        ]

    def read_shardlist(self):
        """Get a list of shards from a string, list, or JSON file."""
        shards = self.args.shards
        if isinstance(shards, str) and shard.endswith(".json"):
            shards, total_size = read_shards_from_json(shards, args)
            self.shardlist = shards
            self.total_size = total_size
        elif isinstance(shards, str):
            self.shardlist = list(braceexpand.braceexpand(shards))
        elif isinstance(shards, list):
            self.shardlist = shards
        else:
            raise ValueError("unknown shard list type")
        assert self.shardlist is not None
        assert len(self.shardlist) > 0

    def iterate_shards(self):
        """Iterate over the shardlist."""
        for i, shard in enumerate(self.shardlist):
            yield dict(url=shard, shard_num=i)

    def split_shards(self, source):
        """Split the shardlist according to a custom function.

        This is used for multiple workers and/or multiple nodes.
        """
        if self.args.shard_split_fn:
            yield from self.args.shard_split_fn(source)
        else:
            yield from source

    def repeat_shards(self, source):
        """Repeat the shards according to the repeats parameter.

        This takes place after splitting the shards, so the repeats
        are per worker or per node.
        """
        shards = list(source)
        for i in range(self.args.repeats):
            for shard in shards:
                yield shard

    def shuffle_shards(self, source):
        """Shuffle the shards."""
        n = self.args.shard_shuffle_size
        yield from filters.shuffle(bufsize=n, initial=n)(source)

    def open_shards(self, source):
        """Open the shards and yield url+stream dicts.

        This optionally uses a cache to store the shards if a
        cache_dir is provided.
        """
        if self.args.cache_dir is None:
            yield from tariterators.url_opener(source)
        else:
            yield from self.cache(source)

    def iterate_tar_files(self, source):
        """Iterate over the files in the tar archives."""
        yield from tariterators.tar_file_expander(source)

    def rename_files(self, source):
        """Rename files according to a custom function."""
        if self.args.file_fn:
            yield from map_expand(source, self.args.file_fn)
        else:
            yield from source

    def group_by_keys(self, source):
        """Group samples by keys using WebDataset conventions."""
        for i, sample in enumerate(tariterators.group_by_keys(source)):
            fix_dots(sample)
            sample["__epoch__"] = self.epoch
            sample["__count__"] = i
            yield sample

    def shuffle_samples(self, source):
        """Shuffle the samples within the stream using shuffle_size."""
        n = self.args.shuffle_size
        yield from filters.shuffle(n)(source)

    def transform_sample(self, sample):
        """Apply the given transformations to the sample."""
        return apply_transformations(self.args.transformations, sample)

    def transform_samples(self, source):
        """Apply the transformations to the stream of samples.

        This can add samples (by returning an iterator) or
        remove samples (by returning None).
        """
        yield from map_expand(source, self.transform_sample)

    def limit_size(self, source):
        """Limit the size of the dataset to args.force_size."""
        if self.args.force_size is not None:
            yield from islice(source, self.args.force_size)
        else:
            yield from source

    def batch_samples(self, source):
        """Batch the samples if a batch_size is given."""
        if self.args.batch_size is None:
            yield from source
            return
        batcher = filters.batched(
            self.args.batch_size, collation_fn=self.args.collation_fn
        )
        yield from batcher(source)

    def get_stats(self):
        """Return the number of cache accesses and misses."""
        if self.cache is None:
            return 0, 0
        return self.cache.accesses, self.cache.misses

    def check_cache_misses(self):
        """Check if the cache miss rate is too high."""
        if self.cache is None:
            return
        accesses, misses = self.get_stats()
        if accesses > 100 and misses / accesses > 0.3:
            warnings.warn(
                "ShardListDataset has a cache miss rate of {:.1%}%".format(
                    misses * 100.0 / accesses
                )
            )

    def __iter__(self):
        """Iterate over the dataset."""
        self.epoch += 1
        set_pipeline_epochs(self.pipeline, self.epoch)
        yield from run_pipeline(self.pipeline)

    def set_size(self, n):
        """Set the size of the dataset."""
        self.total_size = n

    def size(self):
        """Return the number of samples in the dataset.

        This is not called __len__ because some PyTorch code checks for the presence
        of that method to determine if the dataset is indexable. Furthermore, the length
        need not be accurate, and for some datasets, we do not know the length.
        """
        return self.total_size

    def close(self):
        """ "Close the dataset."""
        for stage in self.pipeline[::-1]:
            if hasattr(stage, "close"):
                stage.close()
            del stage
        self.cache.clear()
        del self.cache
