# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, Iterator, List, Optional, Union
from zipfile import ZipFile

import numpy
import pandas
from bs4 import BeautifulSoup, Tag
from pandas import DataFrame, Int64Dtype
from tqdm import tqdm
from unidecode import unidecode

from .cast import column_converters, isna, safe_int_cast
from .constants import GLOBAL_DISABLE_PROGRESS


def fuzzy_text(text: str, remove_regex: str = r"[^a-z\s]", remove_spaces: bool = True):
    # TODO: handle bad inputs (like empty text)
    text = unidecode(str(text)).lower()
    for token in ("y", "and", "of"):
        text = re.sub(f" {token} ", " ", text)
    text = re.sub(remove_regex, "", text)
    text = re.sub(r"^county ", "", text)
    text = re.sub(r" county$", "", text)
    text = re.sub(r"^region ", "", text)
    text = re.sub(r" region$", "", text)
    text = re.sub(r"^borough ", "", text)
    text = re.sub(r" borough$", "", text)
    text = re.sub(r"^province ", "", text)
    text = re.sub(r" province$", "", text)
    text = re.sub(r"^department ", "", text)
    text = re.sub(r" department$", "", text)
    text = re.sub(r"^district ", "", text)
    text = re.sub(r" district$", "", text)
    text = re.sub(r"\s+", "" if remove_spaces else " ", text)
    return text.strip()


def parse_dtype(dtype_name: str) -> Any:
    """
    Parse a dtype name into its pandas name. Only the following dtypes are supported in
    our table schemas:

    | column type label | pandas dtype |
    | ----------------- | ------------ |
    | str               | str          |
    | int               | Int64        |
    | float             | float        |

    Arguments:
        dtype_name: label of the dtype object
    Returns:
        type: dtype object
    """
    if dtype_name == "str":
        return "str"
    if dtype_name == "int":
        return Int64Dtype()
    if dtype_name == "float":
        return "float"
    raise TypeError(f"Unsupported dtype: {dtype_name}")


def read_file(path: Union[Path, str], file_type: str = None, **read_opts) -> DataFrame:
    ext = file_type or str(path).split(".")[-1]

    # Keep a list of known extensions here so we don't forget to update it
    known_extensions = ("csv", "json", "html", "xls", "xlsx", "zip")

    # Hard-code a set of sensible defaults to reduce the amount of magic Pandas provides
    default_read_opts = {"keep_default_na": False, "na_values": ["", "N/A"]}

    if ext == "csv":
        return pandas.read_csv(path, **{**default_read_opts, **read_opts})
    if ext == "json":
        return pandas.read_json(path, **read_opts)
    if ext == "html":
        with open(path, "r") as fd:
            return read_html(fd.read(), **read_opts)
    if ext == "xls" or ext == "xlsx":
        return pandas.read_excel(path, **{**default_read_opts, **read_opts})
    if ext == "zip":
        with TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            with ZipFile(path, "r") as archive:
                if "file_name" in read_opts:
                    file_name = read_opts.pop("file_name")
                else:
                    file_name = next(
                        name
                        for name in archive.namelist()
                        if name.rsplit(".", 1)[-1] in known_extensions
                    )
                archive.extract(file_name, tmpdir)
                return read_file(tmpdir / file_name, **read_opts)

    raise ValueError("Unrecognized extension: %s" % ext)


def read_lines(path: Path, mode: str = "r", skip_empty: bool = False) -> Iterator[str]:
    """
    Efficiently reads a line by line and closes it using a context manager.

    Arguments:
        path: Path of the file to read
    Returns:
        Iterator[str]: Each line of the file
    """
    with path.open(mode) as fd:
        for line in fd:
            if skip_empty and (not line or line.isspace()):
                continue
            yield line


def read_table(path: Union[Path, str], schema: Dict[str, Any] = None, **read_opts) -> DataFrame:
    """
    Schema-aware version of `read_file` which converts the columns to the appropriate type
    according to the given schema.

    Arguments:
        schema: Dictionary of <column, dtype>
    Returns:
        Callable[[Union[Path, str]], DataFrame]: Function like `read_file`
    """
    return read_file(path, converters=column_converters(schema or {}), **read_opts)


def _get_html_columns(row: Tag) -> List[Tag]:
    cols = []
    for elem in filter(lambda row: isinstance(row, Tag), row.children):
        cols += [elem] * (safe_int_cast(elem.attrs.get("colspan", 1)) or 1)
    return list(cols)


def _default_html_cell_parser(cell: Tag, row_idx: int, col_idx: int):
    return cell.get_text().strip()


def count_html_tables(html: str, selector: str = "table"):
    page = BeautifulSoup(html, "lxml")
    return len(page.select(selector))


def wiki_html_cell_parser(cell: Tag, row_idx: int, col_idx: int):
    return re.sub(r"\[.+\]", "", cell.get_text().strip())


def read_html(
    html: str,
    selector: str = "table",
    table_index: int = 0,
    skiprows: int = 0,
    header: bool = False,
    parser: Callable = None,
) -> DataFrame:
    """ Parse an HTML table into a DataFrame """
    parser = parser if parser is not None else _default_html_cell_parser

    # Fetch table and read its rows
    page = BeautifulSoup(html, "lxml")
    table = page.select(selector)[table_index]
    rows = [_get_html_columns(row) for row in table.find_all("tr")]

    # Adjust for rowspan > 1
    for idx_row, row in enumerate(rows):
        for idx_cell, cell in enumerate(row):
            rowspan = int(cell.attrs.get("rowspan", 1))
            cell.attrs["rowspan"] = 1  # reset to prevent cascading
            for offset in range(1, rowspan):
                rows[idx_row + offset].insert(idx_cell, cell)

    # Get text within table cells and build dataframe
    records = []
    for row_idx, row in enumerate(rows[skiprows:]):
        records.append([parser(elem, row_idx, col_idx) for col_idx, elem in enumerate(row)])
    data = DataFrame.from_records(records)

    # Parse header if requested
    if header:
        data.columns = data.iloc[0]
        data = data.drop(data.index[0])

    return data


def _dtype_formatter(dtype: Any) -> Callable[[Any], str]:
    """
    Parse a dtype name and output the formatter used for printing it as a string.

    Arguments:
        dtype: dtype object.
    Returns:
        str: formatting string.
    """

    if dtype == "str" or dtype == str:
        return lambda val: str(val)
    if dtype == "float" or dtype == float:
        return lambda val: round(val, 6)
    if dtype == "int" or isinstance(dtype, Int64Dtype):
        return lambda val: "%d" % val
    raise TypeError(f"Unsupported dtype: {dtype}")


def _format_call(format_func: Callable[[Any], str], val: Any) -> str:
    """
    Wrap the format function call to return empty string when value is null.
    Arguments:
        format_func: Formatting function.
        val: Value to be formatted.
    Returns:
        str: Empty string if val is null, otherwise the result of `format_func(val)`.
    """
    return "" if isna(val, skip_pandas_nan=True) else format_func(val)


def export_csv(
    data: DataFrame, path: Union[Path, str] = None, schema: Dict[str, Any] = None, **csv_opts
) -> Optional[str]:
    """
    Exports a DataFrame to CSV using consistent options. This function will modify fields of the
    input DataFrame in place to format them for output, consider making a copy prior to passing the
    data into this function.
    Arguments:
        data: DataFrame to be output as CSV.
        path: Location on disk to write the CSV to.
        schema: Dictionary of <column, dtype>.
        csv_opts: Additional options passed to the `DataFrame.to_csv()` method.
    Returns:
        Optional[str]: The CSV output as a string, if no path is provided.
    """

    # Path may be None which means output CSV gets returned as a string
    if path is not None:
        path = str(path)

    # Get the CSV file header from schema if provided, otherwise use data columns
    header = schema.keys() if schema is not None else data.columns
    header = [column for column in header if column in data.columns]

    if schema is None:
        formatters = {col: _dtype_formatter(str) for col in header}
    else:
        formatters = {col: _dtype_formatter(dtype) for col, dtype in schema.items()}

    # Convert all columns to appropriate type
    for column, converter in column_converters(schema or {}).items():
        if column in header:
            converter = partial(converter, skip_pandas_nan=True)
            data[column] = data[column].fillna(numpy.nan).apply(converter)

    # Format the data as a string one column at a time
    data_fmt = DataFrame(columns=header, index=data.index)
    for column, format_func in formatters.items():
        if column in header:
            map_func = partial(_format_call, format_func)
            data_fmt[column] = data[column].apply(map_func)

    return data_fmt.to_csv(path_or_buf=path, index=False, **csv_opts)


def pbar(*args, **kwargs) -> tqdm:
    """
    Helper function used to display a tqdm progress bar respecting global settings for whether all
    progress bars should be disabled. All arguments are passed through to tqdm but the "disable"
    option is set accordingly.
    """
    return tqdm(*args, **{**kwargs, **{"disable": os.getenv(GLOBAL_DISABLE_PROGRESS)}})


@contextmanager
def display_progress(enable: bool):
    """
    Provide a context manager so users don't have to touch global variables to disable progress.
    """
    try:
        # Set the disable progress flag
        if not enable:
            progress_env_value = os.getenv(GLOBAL_DISABLE_PROGRESS)
            os.environ[GLOBAL_DISABLE_PROGRESS] = "1"
        yield None
    finally:
        # Reset the disable progress flag
        if not enable:
            if progress_env_value is None:
                os.unsetenv(GLOBAL_DISABLE_PROGRESS)
            else:
                os.environ[GLOBAL_DISABLE_PROGRESS] = progress_env_value
