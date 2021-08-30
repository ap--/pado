"""tile classes for pado images"""
from __future__ import annotations

from typing import Iterator
from typing import Optional
from typing import TYPE_CHECKING
from typing import Tuple

import numpy as np
from shapely.geometry import Polygon

from pado._compat import cached_property
from pado.images.utils import IntPoint
from pado.images.utils import MPP

if TYPE_CHECKING:
    from pado.images.image import Image
    from pado.images.utils import IntSize


class Tile:
    """pado.img.Tile abstracts rectangular regions in whole slide image data"""

    def __init__(
            self,
            mpp: Tuple[float, float],
            bounds: Tuple[int, int, int, int],
            data: Optional[np.ndarray] = None,
            parent: Optional[Image] = None,
    ):
        self._mpp = mpp
        self._bounds = bounds
        self.data: Optional[np.ndarray] = data
        self.parent: Optional[Image] = parent

    @cached_property
    def mpp(self) -> MPP:
        return MPP.from_tuple(self._mpp)

    @cached_property
    def size(self) -> IntSize:
        b = self._bounds
        return IntSize.from_tuple((b[2] - b[0], b[3] - b[1]), mpp=self.mpp)

    @cached_property
    def x0y0(self) -> IntPoint:
        return IntPoint.from_tuple(self._bounds[:2], mpp=self.mpp)

    def shape(self, mpp: Optional[MPP] = None) -> Polygon:
        if mpp is None:
            return Polygon.from_bounds(*self._bounds)
        else:
            raise NotImplementedError("todo: allow shape scaling")


class TileIterator:
    """helper class to iterate over tiles

    Note: we should subclass to enable all sorts of fancy tile iteration

    """
    def __init__(
        self,
        image: Image,
        *,
        size: IntSize,
        level: int,
    ):
        """create a tile iterator instance"""
        if not isinstance(image, Image):
            raise TypeError(f"expected Image, got {image!r} of type {type(image).__name__}")
        if not isinstance(size, IntSize):
            raise TypeError(f"expected IntSize, got {size!r} of type {type(size).__name__}")
        if not 0 <= int(level) < image.level_count:
            raise ValueError("level={self.level} not in range({self.image.level_count})")
        self.image: Image = image
        self.size: IntSize = size
        self.level: int = int(level)

    def __iter__(self) -> Iterator[Tile]:
        """return a plain iterator with no overlap over all tiles of the image

        Note: boundary tiles that don't meet the size requirements are discarded
        """
        img_lvl = self.image.level_dimensions[self.level]
        tile_size = self.size
        img = self.image

        # todo: incomplete tiles at borders are currently discarded
        x, y = np.mgrid[
            0: img_lvl.width - tile_size.width + 1: tile_size.width,
            0: img_lvl.height - tile_size.height + 1: tile_size.height,
        ]

        # todo: check if this ordering makes sense? maybe depend on chunk order in zarr
        bounds = np.hstack((
            x.reshape(-1, 1),
            x.reshape(-1, 1) + tile_size.width,
            y.reshape(-1, 1),
            y.reshape(-1, 1) + tile_size.height,
        ))

        mpp_xy = self.image.level_mpp[self.level].as_tuple()
        z_array = self.image.get_zarr(self.level)

        return (
            Tile(mpp=mpp_xy, bounds=(x0, x1, y0, y1), data=z_array[y0:y1, x0:x1], parent=img)
            for x0, x1, y0, y1 in bounds
        )