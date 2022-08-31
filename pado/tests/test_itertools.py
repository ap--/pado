from __future__ import annotations

import warnings
from unittest import mock

import pytest

from pado.dataset import PadoItem
from pado.images.tiles import FastGridTiling
from pado.images.tiles import PadoTileItem
from pado.images.utils import MPP
from pado.itertools import RetryErrorHandler
from pado.itertools import SlideDataset
from pado.itertools import TileDataset
from pado.mock import mock_dataset


@pytest.fixture
def ds_iter():
    yield mock_dataset(None, num_images=7)


def test_slide_dataset(ds_iter):
    slide_ds = SlideDataset(ds_iter)

    for idx in range(len(slide_ds)):
        item = slide_ds[idx]
        assert isinstance(item, PadoItem)
        assert item.id is not None
        assert item.image is not None


def test_tile_dataset(ds_iter):
    tile_ds = TileDataset(
        ds_iter,
        tiling_strategy=FastGridTiling(
            tile_size=(10, 10),
            target_mpp=MPP(1, 1),
            overlap=0,
            min_chunk_size=0.0,  # use 0.2 or so with real data
            normalize_chunk_sizes=True,
        ),
    )

    with warnings.catch_warnings(record=True) as rec:
        tile_ds.precompute_tiling()
        # check warnings
        for w in rec:
            assert "all chunksizes identical" in str(w.message)

    for idx in range(len(tile_ds)):
        item = tile_ds[idx]
        assert isinstance(item, PadoTileItem)
        assert item.id is not None
        assert item.tile is not None
        assert item.tile.sum() > 0


def test_retry_handler(ds_iter, monkeypatch):

    retry_handler = RetryErrorHandler(
        ZeroDivisionError,  # just used as an example here
        retry_delay=1.0,
        total_delay=30.0,
        exponential_backoff=True,
    )
    tile_ds = TileDataset(
        ds_iter,
        tiling_strategy=FastGridTiling(
            tile_size=(10, 10),
            target_mpp=MPP(1, 1),
            overlap=0,
            min_chunk_size=0.0,
            normalize_chunk_sizes=True,
        ),
        error_handler=retry_handler,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tile_ds.precompute_tiling()

    sleep_mock = mock.Mock()
    monkeypatch.setattr(retry_handler, "_sleep", sleep_mock)
    monkeypatch.setattr(type(ds_iter), "__getitem__", lambda *_: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        _ = tile_ds[0]

    # check that retry handler was used
    called_delays = [c[0][0] for c in sleep_mock.call_args_list]
    assert sleep_mock.call_count == 4
    assert called_delays == [1.0, 2.0, 4.0, 8.0]