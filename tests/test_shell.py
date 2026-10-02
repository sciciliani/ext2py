import io

import pytest

from ext2py.cli import main
from ext2py.reader import ExtFS
from ext2py.shell import ExtShell
from imagebuilder import ImageBuilder


def make_image() -> bytes:
    b = ImageBuilder(block_size=1024)
    b.mkdir("/etc")
    b.file("/etc/hostname", b"box\n")
    b.mkdir("/home")
    b.mkdir("/home/me")
    b.file("/home/me/notes.txt", b"remember\n")
    b.file("/home/me/.hidden", b"")
    b.symlink("/home/me/host", "/etc/hostname")
    return b.build()


@pytest.fixture
def shell(tmp_path):
    out = io.StringIO()
    sh = ExtShell(ExtFS(io.BytesIO(make_image())), stdin=io.StringIO(), stdout=out)
    sh.local_dir = str(tmp_path)

    def run(*lines):
        out.seek(0)
        out.truncate()
        for line in lines:
            sh.onecmd(line)
        return out.getvalue()

    sh.run = run
    return sh


def test_cd_ls_pwd(shell):
    assert shell.run("ls") == "etc/\nhome/\n"
    assert shell.run("cd home/me", "pwd") == "/home/me\n"
    assert shell.run("ls") == "host@\nnotes.txt\n"
    assert ".hidden" in shell.run("ls -a")
    assert shell.run("cd ..", "pwd") == "/home\n"
    assert shell.run("cd", "pwd") == "/\n"


def test_ls_long(shell):
    out = shell.run("ls -l /home/me")
    assert "-rw-r--r--" in out and "notes.txt" in out
    assert "host -> /etc/hostname" in out


def test_errors_do_not_crash(shell):
    assert "No such file" in shell.run("cd /nope")
    assert "Not a directory" in shell.run("cd /etc/hostname")
    assert "unknown command" in shell.run("frobnicate")
    assert shell.run("pwd") == "/\n"


def test_cat_and_stat(shell):
    assert shell.run("cat /home/me/host") == "box\n"
    assert "Type: symlink" in shell.run("stat /home/me/host")
    assert "Filesystem:     ext2" in shell.run("info")


def test_find_and_tree(shell):
    assert shell.run("find / -name '*.txt'") == "/home/me/notes.txt\n"
    assert "└── me/" in shell.run("tree /home")


def test_get(shell, tmp_path):
    shell.run("cd /home/me", "get notes.txt")
    assert (tmp_path / "notes.txt").read_bytes() == b"remember\n"
    shell.run("get /home ./copy")
    assert (tmp_path / "copy" / "me" / "notes.txt").read_bytes() == b"remember\n"
    (tmp_path / "dl").mkdir()
    shell.run("lcd dl", "get /etc")
    assert (tmp_path / "dl" / "etc" / "hostname").read_bytes() == b"box\n"


def test_completion(shell):
    assert shell.complete_cd("ho", "cd ho", 3, 5) == ["home/"]
    assert shell.complete_ls("/home/me/n", "", 0, 0) == ["/home/me/notes.txt"]
    shell.run("cd /home")
    assert shell.complete_get("me/h", "", 0, 0) == ["me/host"]


def test_cli(tmp_path, capsys):
    img = tmp_path / "fs.img"
    img.write_bytes(make_image())
    assert main(["ls", str(img), "/home/me"]) == 0
    assert capsys.readouterr().out == "host@\nnotes.txt\n"
    assert main(["info", str(img)]) == 0
    assert "testvol" in capsys.readouterr().out
    assert main(["get", str(img), "/etc", str(tmp_path)]) == 0
    assert (tmp_path / "etc" / "hostname").read_bytes() == b"box\n"
    assert main(["ls", str(img), "/missing"]) == 1
    assert "No such file" in capsys.readouterr().err
    assert main(["parts", str(img)]) == 0
