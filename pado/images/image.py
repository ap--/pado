"""pado image abstraction to hide image loading implementation"""
from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import TYPE_CHECKING
from typing import Tuple
from typing import Union

import tiffslide
import zarr.core
from fsspec import get_fs_token_paths
from fsspec.core import OpenFile
from pydantic import BaseModel
from pydantic import ByteSize
from pydantic import Extra
from pydantic import PositiveFloat
from pydantic import PositiveInt
from pydantic import validator
from pydantic.color import Color
from tiffslide import TiffSlide

from pado.images.utils import IntPoint
from pado.images.utils import IntSize
from pado.images.utils import MPP
from pado.io.files import urlpathlike_to_fsspec
from pado.io.files import urlpathlike_to_string
from pado.types import UrlpathLike

if TYPE_CHECKING:
    import PIL
    import numpy as np


_log = logging.getLogger(__name__)


# --- metadata and info models ---

class ImageMetadata(BaseModel):
    """the common image metadata"""
    # essentials
    width: int
    height: int
    objective_power: str  # todo
    mpp_x: PositiveFloat
    mpp_y: PositiveFloat
    downsamples: List[PositiveFloat]
    vendor: Optional[str] = None
    # optionals
    comment: Optional[str] = None
    quickhash1: Optional[str] = None
    background_color: Optional[Color] = None
    bounds_x: Optional[PositiveInt] = None
    bounds_y: Optional[PositiveInt] = None
    bounds_width: Optional[PositiveInt] = None
    bounds_height: Optional[PositiveInt] = None
    # extra
    extra_json: Optional[str] = None

    @validator('downsamples', pre=True)
    def downsamples_as_list(cls, v):
        # this is stored as array in parquet
        return list(v)


class FileInfo(BaseModel):
    """information related to the file on disk"""
    size_bytes: ByteSize
    md5_computed: Optional[str] = None
    time_last_access: Optional[datetime] = None
    time_last_modified: Optional[datetime] = None
    time_status_changed: Optional[datetime] = None


class PadoInfo(BaseModel):
    """information regarding the file loading"""
    urlpath: str
    pado_image_backend: str
    pado_image_backend_version: str


class _SerializedImage(ImageMetadata, FileInfo, PadoInfo):
    class Config:
        extra = Extra.forbid


class Image:
    """pado.img.Image is a wrapper around whole slide image data"""
    __slots__ = (
        'urlpath', '_metadata', '_file_info', '_ctx', '_slide'
    )  # prevent attribute errors during refactor
    __fields__ = _SerializedImage.__fields__

    def __init__(
        self,
        urlpath: UrlpathLike,
        *,
        load_metadata: bool = False,
        load_file_info: bool = False,
        checksum: bool = False,
    ):
        """instantiate an image from an urlpath"""
        self.urlpath = urlpath
        self._metadata: Optional[ImageMetadata] = None
        self._file_info: Optional[FileInfo] = None

        # file handles
        self._ctx: Optional[ExitStack] = None
        self._slide: Optional[TiffSlide] = None

        # optional load on init
        if load_metadata or load_file_info or checksum:
            with self:
                if load_metadata:
                    self._metadata = self._load_metadata()
                if load_file_info or checksum:
                    self._file_info = self._load_file_info(checksum=checksum)

    @classmethod
    def from_obj(cls, obj) -> Image:
        """instantiate an image from an object, i.e. a pd.Series"""
        md = _SerializedImage.parse_obj(obj)
        # get metadata
        metadata = ImageMetadata.parse_obj(md)
        file_info = FileInfo.parse_obj(md)
        pado_info = PadoInfo.parse_obj(md)
        # get extra data
        inst = cls(pado_info.urlpath)
        inst._metadata = metadata
        inst._file_info = file_info
        # todo: warn if tiffslide version difference
        # pado_info ...
        return inst

    def to_record(self) -> dict:
        """return a record for serializing """
        pado_info = PadoInfo(
            urlpath=urlpathlike_to_string(self.urlpath),
            pado_image_backend=TiffSlide.__class__.__qualname__,
            pado_image_backend_version=tiffslide.__version__,
        )
        return _SerializedImage.parse_obj({
            **pado_info.dict(),
            **self.metadata.dict(),
            **self.file_info.dict(),
        }).dict()

    def __enter__(self) -> Image:
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def open(self) -> Image:
        """open an image instance"""
        if not self._ctx:
            self._ctx = ctx = ExitStack()
            try:
                open_file = urlpathlike_to_fsspec(self.urlpath)
                file_obj = ctx.enter_context(open_file)
                # noinspection PyTypeChecker
                self._slide = ctx.enter_context(TiffSlide(file_obj))
            except Exception as e:
                _log.error(f"{self.urlpath!r} with error {e!r}")
                self.close()
                raise
        return self

    def close(self):
        """close and image instance"""
        if self._ctx:
            self._ctx.close()
            self._slide = None
            self._ctx = None

    def __repr__(self):
        return f"{type(self).__name__}({self.urlpath!r})"

    def __eq__(self, other: Any) -> bool:
        """compare if two images are identical"""
        if not isinstance(other, Image):
            return False
        # if checksum available for both
        if self.file_info.md5_computed and other.file_info.md5_computed:
            return self.file_info.md5_computed == other.file_info.md5_computed
        if self.file_info.size_bytes != other.file_info.size_bytes:
            return False
        return self.metadata == other.metadata

    def _load_metadata(self, *, force: bool = False) -> ImageMetadata:
        """load the metadata from the file"""
        if self._metadata is None or force:
            if self._slide is None:
                raise RuntimeError(f"{self!r} not opened and not in context manager")

            slide = self._slide
            props = slide.properties
            dimensions = slide.dimensions

            _used_keys: Dict[str, Any] = {}
            def pget(key): return _used_keys.setdefault(key, props.get(key))

            return ImageMetadata(
                width=dimensions[0],
                height=dimensions[1],
                objective_power=pget(tiffslide.PROPERTY_NAME_OBJECTIVE_POWER),
                mpp_x=pget(tiffslide.PROPERTY_NAME_MPP_X),
                mpp_y=pget(tiffslide.PROPERTY_NAME_MPP_Y),
                downsamples=list(slide.level_downsamples),
                vendor=pget(tiffslide.PROPERTY_NAME_VENDOR),
                background_color=pget(tiffslide.PROPERTY_NAME_BACKGROUND_COLOR),
                quickhash1=pget(tiffslide.PROPERTY_NAME_QUICKHASH1),
                comment=pget(tiffslide.PROPERTY_NAME_COMMENT),
                bounds_x=pget(tiffslide.PROPERTY_NAME_BOUNDS_X),
                bounds_y=pget(tiffslide.PROPERTY_NAME_BOUNDS_Y),
                bounds_width=pget(tiffslide.PROPERTY_NAME_BOUNDS_WIDTH),
                bounds_height=pget(tiffslide.PROPERTY_NAME_BOUNDS_HEIGHT),
                extra_json=json.dumps({
                    key: value for key, value in sorted(props.items())
                    if key not in _used_keys
                })
            )
        else:
            return self._metadata

    def _load_file_info(self, *, force: bool = False, checksum: bool = False) -> FileInfo:
        """load the file information from the file"""
        if self._file_info is None or force:
            if self._slide is None:
                raise RuntimeError(f"{self!r} not opened and not in context manager")

            if isinstance(self.urlpath, str):
                fs, _, [path] = get_fs_token_paths(self.urlpath)
            elif isinstance(self.urlpath, OpenFile):
                fs = self.urlpath.fs
                path = self.urlpath.path
            elif isinstance(self.urlpath, os.PathLike):
                fs, _, [path] = get_fs_token_paths(os.fspath(self.urlpath))
            else:
                raise NotImplementedError(f"todo: {self.urlpath!r} of type {type(self.urlpath)!r}")

            if checksum:
                _checksum = fs.checksum(path)
            else:
                _checksum = None

            info = fs.info(path)
            return FileInfo(
                size_bytes=info['size'],
                md5_computed=_checksum,
                time_last_access=info.get('atime'),
                time_last_modified=info.get('mtime'),
                time_status_changed=info.get('created'),
            )
        else:
            return self._file_info

    @property
    def metadata(self) -> ImageMetadata:
        """the image metadata"""
        if self._metadata is None:
            # we need to load the image metadata
            if self._slide is None:
                raise RuntimeError(f"{self!r} not opened and not in context manager")
            self._metadata = self._load_metadata()
        return self._metadata

    @property
    def file_info(self) -> FileInfo:
        """stats regarding the image file"""
        if self._file_info is None:
            # we need to load the file_info data
            if self._slide is None:
                raise RuntimeError(f"{self!r} not opened and not in context manager")
            self._file_info = self._load_file_info(checksum=False)
        return self._file_info

    @property
    def level_count(self) -> int:
        if self._slide is None:
            raise RuntimeError(f"{self!r} not opened and not in context manager")
        return self._slide.level_count

    @property
    def level_dimensions(self) -> Dict[int, IntSize]:
        if self._slide is None:
            raise RuntimeError(f"{self!r} not opened and not in context manager")
        dims = self._slide.level_dimensions
        down = self._slide.level_downsamples
        mpp0 = self.mpp
        return {
            lvl: IntSize(x, y, mpp0.scale(ds))
            for lvl, ((x, y), ds) in enumerate(zip(dims, down))
        }

    @property
    def level_mpp(self) -> Dict[int, MPP]:
        if self._slide is None:
            raise RuntimeError(f"{self!r} not opened and not in context manager")
        return {
            lvl: self.mpp.scale(ds)
            for lvl, ds in enumerate(self._slide.level_downsamples)
        }

    @property
    def mpp(self) -> MPP:
        return MPP(self.metadata.mpp_x, self.metadata.mpp_y)

    @property
    def dimensions(self) -> IntSize:
        return IntSize(
            x=self.metadata.width,
            y=self.metadata.height,
            mpp=self.mpp,
        )

    def get_thumbnail(self, size: Union[IntSize, Tuple[int, int]]) -> PIL.Image.Image:
        if self._slide is None:
            raise RuntimeError(f"{self!r} not opened and not in context manager")
        if isinstance(size, tuple):
            _, _ = size
        elif isinstance(size, IntSize):
            size = size.as_tuple()
        else:
            raise TypeError(f"expected tuple or IntSize, got {size!r} of cls {type(size).__name__}")
        return self._slide.get_thumbnail(size=size, use_embedded=True)

    def get_array(
        self,
        location: IntPoint,
        region: IntSize,
        level: int,
        *,
        runtime_type_checks: bool = True
    ) -> np.ndarray:
        """return array from a defined level"""
        if runtime_type_checks:
            if self._slide is None:
                raise RuntimeError(f"{self!r} not opened and not in context manager")

            # location
            if not isinstance(location, IntPoint):
                raise TypeError(
                    f"location requires IntPoint, got: {location!r} of {type(location).__name__}"
                )
            elif location.mpp is not None and location.mpp != self.mpp:
                _guess = next(  # improve error for user
                    (idx for idx, mpp in self.level_mpp.items() if mpp == location.mpp),
                    'level-not-in-image'
                )
                raise ValueError(f"location not at level 0, got {location!r} at {_guess}")

            # level (indirectly)
            try:
                level_mpp = self.level_mpp[level]
            except KeyError:
                raise ValueError(f"level error: 0 <= {level} <= {self.level_count}")

            # region
            if not isinstance(region, IntSize):
                raise TypeError(
                    f"region requires IntSize, got: {region!r} of {type(region).__name__}"
                )
            elif region.mpp is not None and region.mpp != level_mpp:
                _guess = next(  # improve error for user
                    (idx for idx, mpp in self.level_mpp.items() if mpp == region.mpp),
                    'level-not-in-image'
                )
                raise ValueError(f"region not at level {level}, got {region!r} at {_guess}")

        if self._slide is None:
            raise RuntimeError(f"{self!r} not opened and not in context manager")

        return self._slide.read_region(
            location.as_tuple(), level, region.as_tuple(), as_array=True
        )

    def get_zarr(self, level: int) -> zarr.core.Array:
        """return the entire level as zarr"""
        if self._slide is None:
            raise RuntimeError(f"{self!r} not opened and not in context manager")
        zgrp = self._slide.ts_zarr_grp
        if isinstance(zgrp, zarr.core.Array):
            if level != 0:
                raise IndexError(f"level {level} not available")
            return zgrp
        elif isinstance(zgrp, zarr.hierarchy.Group):
            return zgrp[str(level)]
        else:
            raise NotImplementedError(f"unexpected instance {zgrp!r} of type {type(zgrp).__name__}")