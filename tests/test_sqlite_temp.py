"""Reproduce SQLite query spill when a temp directory is denied by confinement."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


@pytest.fixture(scope='module')
def deny_sqlite_temp(tmp_path_factory):
    compiler = shutil.which('gcc')
    if sys.platform != 'linux' or not compiler:
        pytest.skip('Linux and gcc are required for the filesystem fault injection')
    directory = tmp_path_factory.mktemp('sqlite-temp')
    source = directory / 'deny.c'
    source.write_text(r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
int open64(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = va_arg(args, int);
        va_end(args);
    }
    if (strncmp(path, "/var/tmp/etilqs_", 16) == 0) {
        fprintf(stderr, "Denied SQLite temporary file: %s\n", path);
        errno = EACCES;
        return -1;
    }
    int (*real_open)(const char *, int, ...) = dlsym(RTLD_NEXT, "open64");
    return real_open(path, flags, mode);
}
''')
    library = directory / 'deny.so'
    subprocess.run([compiler, '-shared', '-fPIC', '-o', str(library), str(source),
                    '-ldl'], check=True, capture_output=True, text=True)
    return library


@pytest.mark.parametrize('dockerfile', ['Dockerfile', 'addon/Dockerfile'])
def test_image_sqlite_temp_directory_survives_denied_var_tmp(dockerfile, deny_sqlite_temp):
    query = '''
import sqlite3
connection = sqlite3.connect(':memory:')
connection.execute('PRAGMA temp_store=FILE')
connection.execute('CREATE TABLE videos(channel INTEGER)')
connection.executemany('INSERT INTO videos VALUES (?)',
                      ((-100123 - i % 2,) for i in range(500000)))
rows = connection.execute(
    'SELECT channel, count(*) AS count FROM videos GROUP BY channel').fetchall()
assert rows == [(-100124, 250000), (-100123, 250000)], rows
'''
    env = dict(os.environ, LD_PRELOAD=str(deny_sqlite_temp), SQLITE_TMPDIR='/var/tmp')
    # The exact dashboard query must fail once sorting spills to the denied path.
    denied = subprocess.run([sys.executable, '-c', query], env=env,
                            capture_output=True, text=True, timeout=30)
    assert denied.returncode != 0
    assert 'Denied SQLite temporary file: /var/tmp/etilqs_' in denied.stderr
    assert 'sqlite3.OperationalError: unable to open database file' in denied.stderr

    image = (Path(__file__).parents[1] / dockerfile).read_text()
    configured = re.search(r'^ENV SQLITE_TMPDIR=(\S+)$', image, re.M)
    assert configured, 'Image must configure the SQLite temp directory'
    env['SQLITE_TMPDIR'] = configured[1]
    permitted = subprocess.run([sys.executable, '-c', query], env=env,
                               capture_output=True, text=True, timeout=30)
    assert permitted.returncode == 0, permitted.stderr
    assert not permitted.stderr
