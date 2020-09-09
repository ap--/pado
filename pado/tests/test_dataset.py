from pathlib import Path

import pandas as pd

from pado.dataset import PadoDataset


def test_pado_test_datasource(datasource):
    with datasource:
        assert isinstance(datasource.metadata, pd.DataFrame)
        for image in datasource.images():
            assert image.id is not None
            assert image.path.is_file()


def test_write_pado_dataset(datasource, tmp_path):

    dataset_path = tmp_path / "my_dataset"

    ds = PadoDataset(dataset_path, mode="x")
    ds.add_source(datasource)

    assert len(list(filter(Path.is_file, (ds.path / "images").glob("**/*")))) == 1
    assert isinstance(ds.metadata, pd.DataFrame)
    assert len(ds.metadata) == 10
