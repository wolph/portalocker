import py
import logging
import pytest


logger = logging.getLogger(__name__)


@pytest.fixture
def tmpfile(tmpdir_factory):
    tmpdir = tmpdir_factory.mktemp('temp')
    filename = tmpdir.join('tmpfile')
    yield str(filename)
    try:
        filename.remove(ignore_errors=True)
    except (py.error.EBUSY, py.error.ENOENT):
        pass
