# This file is part of qserv.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import backoff
from contextlib import closing
from dataclasses import dataclass
import gzip
import json
import logging
import mysql.connector
from mysql.connector.cursor import MySQLCursor
import os
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Dict, Generator, List, NamedTuple, Optional, Sequence, Union
import yaml

from .qserv_backoff import on_backoff, max_backoff_sec
from .mysql_connection import mysql_connection
from .replicationInterface import ReplicationInterface
from .template import apply_template_cfg

qserv_data_dir = "/qserv/data"

chunk_info_file = "chunk_info.json"

_log = logging.getLogger(__name__)


def unzip(source: str, destination: str) -> None:
    """Unzip a source file and write it to the target location."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with gzip.open(source, "rb") as _source, open(destination, "wb") as _target:
        shutil.copyfileobj(_source, _target)


def execute(cursor: MySQLCursor, stmt: str, multi: bool = False) -> None:
    """Execute a statement in a cursor, and clean up the cursor so it can be
    closed without causing warnings on the server."""
    warnings = []
    results = []
    # cursors may contain results objects (if multi==True) or a MySQLCursor (if
    # multi==False)
    cursors = []

    if multi:
        cursors = cursor.execute(stmt, multi=True)
    else:
        cursor.execute(stmt, multi=False)
        cursors = [cursor]

    for cursor in cursors:
        if cursor.with_rows:
            for row in cursor.fetchall():
                results.append(row)
            if (w := cursor.fetchwarnings()):
                warnings.append(w)

    if warnings:
        _log.warn("Warnings were issued when executing \"%s\": %s", stmt, warnings)
    if results:
        _log.info("Results were returned when executing \"%s\": %s", results, stmt)


@backoff.on_exception(
    exception=mysql.connector.errors.DatabaseError,
    wait_gen=backoff.expo,
    on_backoff=on_backoff(log=_log),
    max_time=max_backoff_sec,
)
def _create_ref_db(ref_db_admin: str, name: str) -> None:
    """Create a database in the mysql used for integration test reference.

    Parameters
    ----------
    ref_db_admin : `str`
        URI to the reference db for the admin user.
    name : `str`
        The name of the database to create.
    """
    statements = [
        "CREATE USER IF NOT EXISTS '{{ mysqld_user_qserv }}'@'localhost';",
        "CREATE USER IF NOT EXISTS '{{ mysqld_user_qserv }}'@'%';",
        "CREATE DATABASE IF NOT EXISTS {name};",
        "GRANT ALL ON {name}.* TO '{{ mysqld_user_qserv }}'@'localhost';",
        "GRANT ALL ON {name}.* TO '{{ mysqld_user_qserv }}'@'%';",
        "FLUSH PRIVILEGES;",
    ]
    for stmt in statements:
        with closing(mysql_connection(uri=ref_db_admin)) as cnx:
            stmt = apply_template_cfg(stmt)
            # `format` must be called after `apply_template_cfg`, because `format`
            # consumes the inner braces (changes '{{ mysql_user_qserv}}' to
            # '{mysql_user_qserv }') and so breaks the jinja templating syntax.
            stmt = stmt.format(name=name)
            _log.debug("_create_ref_db stmt:%s", stmt)
            with closing(cnx.cursor()) as cursor:
                execute(cursor, stmt)


def _create_ref_table(cnx: mysql.connector.connection, db: str, schema_file: str) -> None:
    """Create a table in the mysql database used for integration test reference.

    Parameters
    ----------
    cnx : `mysql.connector.connection`
        Connection to the reference database that has permission to create a
        table.
    db : `str`
        The name of the database to create the table in.
    schema_file : `str`
        Absolute path to a file that contains the schema used to create the
        table.
    """
    _log.debug("_create_ref_table in db %s schema_file:%s", db, schema_file)
    if not cnx.is_connected():
        cnx.connect()
    with closing(cnx.cursor()) as cursor:
        stmt = f"USE {db}"
        execute(cursor, stmt)
        with open(schema_file) as f:
            execute(cursor, f.read(), multi=True)


def _load_ref_data(
    cnx: mysql.connector.connection, data_file: str, db: str, table: str, field_sep: str
) -> None:
    """Load database data into the reference database.

    Parameters
    ----------
    cnx : `mysql.connector.connection`
        Connection to the reference database that has permission to load data
        into the table.
    data_file : `str`
        The path to the file that contains data to be loaded via LOAD DATA LOCAL
        INFILE.
    db : `str`
        The name of the database that the table is in.
    table : `str`
        The name of the table to load data into.
    field_sep : `str`
        The data separator used in the data_file.
    """
    sql = f"LOAD DATA LOCAL INFILE '{data_file}' INTO TABLE {table} FIELDS TERMINATED BY '{field_sep}'"
    _log.debug("_load_ref_data sql:%s", sql)
    if not cnx.is_connected():
        cnx.connect()
    with closing(cnx.cursor()) as cursor:
        stmt = f"USE {db}"
        execute(cursor, stmt)
        execute(cursor, sql)
        _log.info(f"inserted {cursor.rowcount} rows into {db}.{table}")


@dataclass
class LoadTable:

    """Contains information about a table to be loaded.

    Parameters
    ----------
    table_name: str
        The name of the table according to the load yaml
    ingest_config: Dict[Any, Any]
        The table ingest config dict
    data_file: str
        The absolute path to the data file (contains the csv or tsv data)
    partition_config_files: List[str]
        The absolute path to the partitioner config files
    data_staging_dir: str
        The location where data can be staged (has "rw" permissions)
    ref_db_table_schema_file: str
        The absolute path to the referecene db table schema file
    """

    table_name: str
    ingest_config: Dict[Any, Any]
    data_file: str
    partition_config_files: List[str]
    data_staging_dir: str
    ref_db_table_schema_file: str

    @property
    def is_partitioned(self) -> bool:
        return bool(self.ingest_config["is_partitioned"])

    @property
    def is_gzipped(self) -> bool:
        return os.path.splitext(self.data_file)[1] == ".gz"


class LoadDb:
    """Contains information about a database to be loaded."""

    class TablePartition(NamedTuple):
        configs_t: List[str]
        output_t: str

    def __init__(self, load_db_cfg: Dict[Any, Any]):
        # The path to the root of the database files.
        self.root = load_db_cfg["root"]

        self.id = load_db_cfg["id"]

        # self.tables is the list of table names to load into the database.
        self.tables = load_db_cfg["tables"]

        # The template for the absolute path to a table's data file.
        self.datafile_t = os.path.join(self.root, load_db_cfg["data"])

        # The template for the absolute path to a table's schema file.
        self.schema_t = os.path.join(self.root, load_db_cfg["schema"])

        # self.table_partition.configs_t contains the possibly templated
        # absolute paths to the partition config files.
        # self.table_partition.output contains the templated absolute path to
        # the output folder for the partitioner.
        self.table_partition = self.TablePartition(
            [os.path.join(self.root, config) for config in load_db_cfg["partition"]["config"]],
            load_db_cfg["partition"]["output"],  # ("output" is an absolute path.)
        )

        # self.ingest_db_cfg is the database ingest config dict.
        with open(os.path.join(self.root, load_db_cfg["ingest"]["database"])) as f:
            self.ingest_db_cfg = yaml.safe_load(f.read())

        # self.ingest_table_t is the templated absolute path to the table ingest
        # config file.
        self.ingest_table_t = os.path.join(self.root, load_db_cfg["ingest"]["table"])

        self.build_table_stats = load_db_cfg.get("build_table_stats", False)

    @property
    def name(self) -> str:
        """Accessor for the name of the database."""
        return str(self.ingest_db_cfg["database"])

    def iter_tables(self) -> Generator[LoadTable, None, None]:
        """Generator to get a LoadTable instance for each table to be loaded."""
        for table_name in self.tables:
            with open(self.ingest_table_t.format(table_name=table_name)) as f:
                ingest_config = json.load(f)
            yield LoadTable(
                table_name,
                ingest_config,
                self.datafile_t.format(table_name=table_name),
                [p.format(table_name=table_name) for p in self.table_partition.configs_t],
                self.table_partition.output_t.format(table_name=table_name),
                self.schema_t.format(table_name=table_name),
            )


def _partition(staging_dir: str, table: LoadTable, data_file: str) -> None:
    """Partition data for qserv ingest using sph-partition.

    Parameters
    ----------
    staging_dir : `str`
        The absolute path to a folder that can be used to stage files
        used during processing that do not need to be kept.
    table : `LoadTable`
        The description of the table to partition.
    data_file : `str`
        The absolute path to the file that contains the table data. (It
        may have been unzipped to a location different than in `table`.)
    """
    # If config file paths are relative, make them absolute with the data file's dirname.
    partition_config_files = [
        f if os.path.isabs(f) else os.path.join(os.path.dirname(table.data_file), f)
        for f in table.partition_config_files
    ]
    os.makedirs(staging_dir)
    args = [
        "sph-partition",
    ]
    for config_file in partition_config_files:
        args.append("--config-file")
        args.append(config_file)
    args.extend(
        [
            f"--in.path={data_file}",
            "--verbose",
            "--mr.num-workers=6",
            "--mr.pool-size=32768",
            "--mr.block-size=16",
            f"--out.dir={staging_dir}",
        ]
    )
    result = subprocess.run(args, stdout=subprocess.PIPE, encoding="utf-8", errors="replace")
    result.check_returncode()
    partition_info = result.stdout
    partition_info = partition_info.replace("-nan", "null")
    with open(os.path.join(staging_dir, chunk_info_file), "w") as f:
        f.write(partition_info)


def _load_database(load_db: LoadDb, ref_db_uri: str, ref_db_admin: str, repl_ctrl_uri: str) -> None:
    """Load a database.

    Parameters
    ----------
    load_db : `LoadDb`
        Contains details about the database to be loaded.
    ref_db_uri : `str`
        URI to the reference db for the non-admin user.
    ref_db_admin : `str`
        URI to the reference db for the admin user.
    repl_ctrl_uri : `str`
        URI to the replication controller.
    """
    _log.info(f"Loading database %s for test %s", load_db.name, load_db.id)
    repl = ReplicationInterface(repl_ctrl_uri)

    @backoff.on_exception(
        exception=ReplicationInterface.CommandError,
        wait_gen=backoff.expo,
        on_backoff=on_backoff(log=_log),
        max_time=max_backoff_sec,
    )
    def do_ingest_database() -> None:
        repl.ingest_database(json.dumps(load_db.ingest_db_cfg))

    do_ingest_database()

    _create_ref_db(ref_db_admin, load_db.name)

    for table in load_db.iter_tables():
        with closing(mysql_connection(ref_db_admin, local_infile=True)) as cnx:
            _create_ref_table(cnx, load_db.name, table.ref_db_table_schema_file)
            # TODO maybe we should be cosuming more info from each case's description.yaml (like field sep)

            with TemporaryDirectory(dir=qserv_data_dir) as tmp_dir:
                if table.is_gzipped:
                    data_file = os.path.join(tmp_dir, os.path.splitext(os.path.basename(table.data_file))[0])
                    unzip(source=table.data_file, destination=data_file)
                else:
                    data_file = table.data_file
                # Create partition the partitioned table data into chunks
                staging_dir = os.path.join(tmp_dir, table.data_staging_dir)
                if table.is_partitioned:
                    _partition(staging_dir, table, data_file)

                # Assume data is either comma separated (with csv) otherwise tab separated.
                data_file_ext = os.path.splitext(data_file)[1]
                _load_ref_data(
                    cnx,
                    data_file,
                    load_db.name,
                    table.table_name,
                    "," if data_file_ext == ".csv" else "\\t",
                )

                @backoff.on_exception(
                    exception=ReplicationInterface.CommandError,
                    wait_gen=backoff.expo,
                    on_backoff=on_backoff(log=_log),
                    max_time=max_backoff_sec,
                )
                def do_ingest_table_config() -> None:
                    repl.ingest_table_config(json.dumps(table.ingest_config))

                do_ingest_table_config()

                transaction_id = repl.start_transaction(load_db.name)
                if table.is_partitioned:
                    repl.ingest_chunks_data(
                        transaction_id=transaction_id,
                        table_name=table.table_name,
                        chunks_folder=staging_dir,
                        chunk_info_file=os.path.join(staging_dir, chunk_info_file),
                    )
                else:
                    repl.ingest_table_data(
                        transaction_id=transaction_id,
                        table_name=table.table_name,
                        data_file=data_file,
                    )
                repl.commit_transaction(transaction_id)

    repl.publish_database(load_db.name)
    if load_db.build_table_stats:
        repl.build_table_stats(load_db.name, load_db.tables)


def _remove_database(case_data: Dict[Any, Any], ref_db_connection: str, repl_ctrl_uri: str) -> None:
    """Remove an integration test database.

    Parameters
    ----------
    case_data : Dict[Any, Any]
        Contains data about the test cases.
    ref_db_connection : str
        URI to the reference db.
    repl_ctrl_uri : str
        URI to the replication controller.
    """
    _log.debug("_remove_database %s", case_data)
    remove_db = LoadDb(case_data)

    repl = ReplicationInterface(repl_ctrl_uri)
    repl.delete_database(remove_db.name, admin=True)

    sql = f"DROP DATABASE IF EXISTS {remove_db.name}"
    with closing(mysql_connection(uri=ref_db_connection)) as cnx:
        with closing(cnx.cursor()) as cursor:
            execute(cursor, sql)


def _get_cases(cases: Optional[List[str]], test_cases_data: List[Dict[Any, Any]]) -> List[Dict[Any, Any]]:
    """Get the test case data for the cases listed in cases.

    Parameters
    ----------
    Same as `cases` and `test_case_data` parameters of `load`.

    Returns
    -------
    selected_cases : `list`
        The test cases from `test_cases_data` that are named in `cases`,
        or all the `test_cases_data` if `cases` does not name cases.

    Raises
    ------
    RuntimeError
        If `cases` names a case that is not in `test_cases_data`.
    """
    if cases:
        db_data = {db["id"]: db for db in test_cases_data}
        try:
            cases_data = [db_data[case] for case in cases]
        except KeyError as e:
            raise RuntimeError(f"{e.args[0]} is not in {test_cases_data}")
    else:
        cases_data = test_cases_data
    return cases_data


def load(
    repl_ctrl_uri: str,
    ref_db_uri: str,
    test_cases_data: List[Dict[Any, Any]],
    ref_db_admin: str,
    load: Optional[bool],
    cases: Optional[List[str]],
    remove: bool = False,
) -> None:
    """Partition and ingest integration test data into qserv.

    Parameters
    ----------
    repl_ctrl_uri : `str`
        URI to the replication controller.
    ref_db_uri : `str`
        URI to the reference db for the non-admin user.
    test_cases_data : `list` [ `dict` ]
        Dicts whose values will be used to initialize a LoadDb class instance.
    ref_db_admin : `str`
        The connection URI for the root/admin user of the reference database.
    load : `bool` or `None`
        True if the database should be loaded, False if not. If `None`, and
        unload == True then will not load the database, otherwise if `None` will
        load the database if it is not yet loaded into qserv (assumes the ref
        database matches the qserv database.)
    cases : `list` [`str`], optional
        Restrict loading to these test cases if provided.
    remove : bool, optional
        Instead of loading the databases, remove them. By default False

    Raises
    ------
    RuntimeError
        If the named database is not in the provided yaml.
    """
    _log.debug("test cases data:\n%s", test_cases_data)
    cases_data = _get_cases(cases, test_cases_data)
    qserv_dbs = ReplicationInterface(repl_ctrl_uri).get_databases()
    for case_data in cases_data:
        load_db = LoadDb(case_data)
        if load == True or (load is None and load_db.name not in qserv_dbs):
            _load_database(load_db, ref_db_uri, ref_db_admin, repl_ctrl_uri)


def remove(
    repl_ctrl_uri: str,
    ref_db_uri: str,
    test_cases_data: List[Dict[Any, Any]],
    ref_db_admin: str,
    cases: Optional[List[str]] = None,
) -> None:
    """Remove integration test data from qserv.

    Parameters
    ----------
    Same as same-named arguments to `load`.
    """
    cases_data = _get_cases(cases, test_cases_data)
    for case_data in cases_data:
        _remove_database(case_data, ref_db_admin, repl_ctrl_uri)
