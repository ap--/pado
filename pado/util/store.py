from __future__ import annotations

import enum
import json
import os
import sys
from abc import ABC
from typing import Any
from typing import Callable
from typing import Dict
from typing import MutableMapping
from typing import Optional
from typing import Tuple

if sys.version_info[:2] >= (3, 10):
    from typing import TypeGuard  # 3.10+
else:
    from typing_extensions import TypeGuard

import fsspec
import pandas as pd
import pyarrow
from pandas.io.parquet import BaseImpl

from pado._version import version as _pado_version
from pado.types import OpenFileLike
from pado.types import UrlpathLike


class StoreType(str, enum.Enum):
    IMAGE = "image"
    METADATA = "metadata"


class Store(ABC):
    METADATA_KEY_PADO_VERSION = 'pado_version'
    METADATA_KEY_STORE_VERSION = 'store_version'
    METADATA_KEY_STORE_TYPE = 'store_type'
    METADATA_KEY_IDENTIFIER = 'identifier'
    METADATA_KEY_USER_METADATA = 'user_metadata'

    USE_NULLABLE_DTYPES = False  # todo: switch to True?
    COMPRESSION = "GZIP"

    def __init__(self, version: int, store_type: StoreType):
        self.version = int(version)
        self.type = store_type

    @property
    def prefix(self):
        return f"pado.{self.type.value}.parquet"

    def _md_set(self, dct: MutableMapping[bytes, bytes], key: str, value: Any) -> None:
        k = f'{self.prefix}.{key}'.encode()  # parquet requires bytes keys
        dct[k] = json.dumps(value).encode()  # string encode value

    def _md_get(self, dct: MutableMapping[bytes, bytes], key: str, default: Any) -> Any:  # require providing a default
        k = f'{self.prefix}.{key}'.encode()
        if k not in dct:
            return default
        return json.loads(dct[k])

    def __metadata_set_hook__(self, dct: Dict[bytes, bytes], setter: Callable[[dict, str, Any], None]) -> None:
        """allows setting more metadata in subclasses"""

    def __metadata_get_hook__(self, dct: Dict[bytes, bytes], getter: Callable[[dict, str, Any], Any]) -> Optional[dict]:
        """allows getting more metadata in subclass or validate versioning"""

    def to_urlpath(self, df: pd.DataFrame, urlpath: UrlpathLike, *, identifier: Optional[str] = None, **user_metadata):
        """store a pandas dataframe with an identifier and user metadata"""
        open_file = urlpathlike_to_fsspec(urlpath, mode="wb")

        BaseImpl.validate_dataframe(df)

        # noinspection PyArgumentList
        table = pyarrow.Table.from_pandas(df, schema=None, preserve_index=None)

        # prepare new schema
        dct: Dict[bytes, bytes] = {}
        self._md_set(dct, self.METADATA_KEY_IDENTIFIER, identifier)
        self._md_set(dct, self.METADATA_KEY_PADO_VERSION, _pado_version)
        self._md_set(dct, self.METADATA_KEY_STORE_VERSION, self.version)
        self._md_set(dct, self.METADATA_KEY_STORE_TYPE, self.type.value)
        if user_metadata:
            self._md_set(dct, self.METADATA_KEY_USER_METADATA, user_metadata)
        dct.update(table.schema.metadata)

        # for subclasses
        self.__metadata_set_hook__(dct, self._md_set)

        # rewrite table schema
        table = table.replace_schema_metadata(dct)

        with open_file as f:
            # write to single output file
            pyarrow.parquet.write_table(
                table, f, compression=self.COMPRESSION,
            )

    def from_urlpath(self, urlpath: UrlpathLike) -> Tuple[pd.DataFrame, str, Dict[str, Any]]:
        """load dataframe and info from urlpath"""
        open_file = urlpathlike_to_fsspec(urlpath, mode="rb")

        to_pandas_kwargs = {}
        if self.USE_NULLABLE_DTYPES:
            mapping = {
                pyarrow.int8(): pd.Int8Dtype(),
                pyarrow.int16(): pd.Int16Dtype(),
                pyarrow.int32(): pd.Int32Dtype(),
                pyarrow.int64(): pd.Int64Dtype(),
                pyarrow.uint8(): pd.UInt8Dtype(),
                pyarrow.uint16(): pd.UInt16Dtype(),
                pyarrow.uint32(): pd.UInt32Dtype(),
                pyarrow.uint64(): pd.UInt64Dtype(),
                pyarrow.bool_(): pd.BooleanDtype(),
                pyarrow.string(): pd.StringDtype(),
            }
            to_pandas_kwargs["types_mapper"] = mapping.get

        table = pyarrow.parquet.read_table(open_file.path, use_pandas_metadata=True, filesystem=open_file.fs)

        # retrieve the additional metadata stored in the parquet
        _md = table.schema.metadata
        identifier = self._md_get(_md, self.METADATA_KEY_IDENTIFIER, None)
        store_version = self._md_get(_md, self.METADATA_KEY_STORE_VERSION, 0)
        store_type = self._md_get(_md, self.METADATA_KEY_STORE_TYPE, None)
        pado_version = self._md_get(_md, self.METADATA_KEY_PADO_VERSION, '0.0.0')
        user_metadata = self._md_get(_md, self.METADATA_KEY_USER_METADATA, {})

        # for subclasses
        get_hook_data = self.__metadata_get_hook__(_md, self._md_get)

        if store_version < self.version:
            raise RuntimeError(
                f"{urlpath} uses Store version={self.version} "
                f"(created with pado=={pado_version}): "
                "please migrate the PadoDataset to a newer version"
            )
        elif store_version > self.version:
            raise RuntimeError(
                f"{urlpath} uses Store version={self.version} "
                f"(created with pado=={pado_version}): "
                "please update pado"
            )

        df = table.to_pandas(**to_pandas_kwargs)
        version_info = {
            self.METADATA_KEY_PADO_VERSION: pado_version,
            self.METADATA_KEY_STORE_VERSION: self.version,
            self.METADATA_KEY_STORE_TYPE: StoreType(store_type),
        }
        user_metadata.update(version_info)
        user_metadata.update(get_hook_data)
        return df, identifier, user_metadata


def is_fsspec_open_file_like(obj: Any) -> TypeGuard[OpenFileLike]:
    """test if an object is like a fsspec.core.OpenFile instance"""
    # if isinstance(obj, fsspec.core.OpenFile) doesn't cut it...
    # ... fsspec filesystems just need to quack OpenFile.
    return (
        isinstance(obj, OpenFileLike)
        and isinstance(obj.fs, fsspec.AbstractFileSystem)
        and isinstance(obj.path, str)
    )


def urlpathlike_to_string(urlpath: UrlpathLike) -> str:
    """convert an urlpath-like object and stringify it"""
    if is_fsspec_open_file_like(urlpath):
        fs: fsspec.AbstractFileSystem = urlpath.fs
        path: str = urlpath.path
        return json.dumps({
            "fs": fs.to_json(),
            "path": path
        })

    if isinstance(urlpath, os.PathLike):
        urlpath = os.fspath(urlpath)

    if isinstance(urlpath, bytes):
        return urlpath.decode()
    elif isinstance(urlpath, str):
        return urlpath
    else:
        raise TypeError(f"can't stringify: {urlpath!r} of type {type(urlpath)!r}")


def urlpathlike_to_fsspec(obj: UrlpathLike, *, mode='rb') -> fsspec.core.OpenFile:
    """use an urlpath-like object and return an fsspec.core.OpenFile"""
    if is_fsspec_open_file_like(obj):
        return obj

    try:
        json_obj = json.loads(obj)  # type: ignore
    except (json.JSONDecodeError, TypeError):
        if isinstance(obj, os.PathLike):
            obj = os.fspath(obj)
        if not isinstance(obj, str):
            raise TypeError(f"got {obj!r} of type {type(obj)!r}")
        return fsspec.open(obj, mode=mode)
    else:
        if not isinstance(json_obj, dict):
            raise TypeError(f"got json {json_obj!r} of type {type(json_obj)!r}")
        fs = fsspec.AbstractFileSystem.from_json(json_obj["fs"])
        return fs.open(json_obj["path"], mode=mode)
