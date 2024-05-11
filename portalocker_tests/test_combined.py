import sys

from portalocker import __main__


def test_combined(tmpdir):
    output_file = tmpdir.join('combined.py')
    __main__.main(['combine', '--output-file', output_file.strpath])

    print(output_file)
    print('#################')
    print(output_file.read())
    print('#################')

    sys.path.append(output_file.dirname)
    # Combined is being generated above but linters won't understand that
    import combined  # type: ignore

    assert combined
