"""Test support shared by the suite.

The repository deliberately carries no pytest-asyncio: the existing async tests
call ``asyncio.run`` inside sync test functions. That works, but it pushes a
wrapper into every test that touches an async API, and the newer service and
tool layers are async throughout.

This hook runs a coroutine test function on a fresh event loop, which is all
pytest-asyncio would do for these tests, without adding a dependency that would
need an image rebuild to install.
"""

import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run a coroutine test function to completion.

    Args:
        pyfuncitem: The collected test.

    Returns:
        bool | None: True when this hook handled the call, None to fall through
        to pytest's normal handling for ordinary functions.
    """
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None

    arguments = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(pyfuncitem.obj(**arguments))
    return True
