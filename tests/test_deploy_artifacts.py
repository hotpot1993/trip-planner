"""钉住镜像与运行配置的一致性。

这里查的不是「Dockerfile 写得好不好看」，而是那几处**错了也不报错**的地方：

- 监听地址漏成 127.0.0.1：容器起得来、日志好看，外面一个请求进不来。
- 时区漏成 UTC：预约提醒算「今天」时差一天，界面上一个字都不提。
- 健康检查探一个不存在的路径：容器永远 unhealthy，而服务本身是好的。
- `.env.local` 没被 `.dockerignore` 排掉：三个真 Key 跟着镜像上了 Docker Hub。
- 装成 site-packages 里的包：`ROOT_DIR` 推错，前端不挂载、库建在镜像里，都不报错。

所以这些都用测试钉住，改动时至少会有声音。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"


def _logical_lines(path: Path) -> list[str]:
    """把反斜杠续行接成一行，否则一条 RUN 会被拆得认不出来。"""
    joined: list[str] = []
    buffer = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1]
            continue
        joined.append(buffer + stripped)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ignore_lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestNothingPrivateEntersTheImage:
    """镜像会被推到 Docker Hub，所以这两样漏出去就是漏给所有人。"""

    @pytest.mark.parametrize("name", [".env.local", ".env"])
    def test_env_files_are_excluded(self, ignore_lines: list[str], name: str) -> None:
        assert name in ignore_lines, f".dockerignore 没排掉 {name}，真 Key 会进镜像"

    def test_the_real_database_is_excluded(self, ignore_lines: list[str]) -> None:
        assert "data/" in ignore_lines, "真实库在 data/ 下，整目录都得排掉"

    def test_nothing_under_data_is_re_included(self, ignore_lines: list[str]) -> None:
        """一个例外都不留。

        这里曾经放行过 `!data/rail_stations.json`（构建要用的车站名表）。那种
        写法有个坑：父目录一旦被排除，`!` 再想放行它里面的文件就是无效的，
        写成 `data/` 便静默失效，然后构建以「文件不存在」失败。

        现在那份表挪到了 `lushu/seed/` 跟版本库一起走，这里也就没有例外可留。
        没有例外，就没有那条规则可以踩。
        """
        re_included = [line for line in ignore_lines if line.startswith("!data")]
        assert not re_included, f"data/ 下不该有任何例外，现在是 {re_included}"

    def test_host_node_modules_are_excluded(self, ignore_lines: list[str]) -> None:
        """宿主机是 Windows，装的是 win32 二进制，拷进 Linux 构建镜像会炸。"""
        assert "web/node_modules" in ignore_lines

    def test_build_context_is_not_copied_wholesale(self, dockerfile: str) -> None:
        instructions = [line.strip() for line in dockerfile.splitlines()]
        assert "COPY . ." not in instructions, (
            "COPY . . 会把 .dockerignore 的每一条都变成唯一防线，逐条显式拷更稳"
        )
        assert [line for line in instructions if line.startswith("COPY ")], (
            "没有解析到任何 COPY，这条测试自己失效了"
        )


class TestRuntimeSettingsThatFailSilently:
    def test_the_service_binds_all_interfaces(self, dockerfile: str) -> None:
        assert "LUSHU_HOST=0.0.0.0" in dockerfile.replace(" ", ""), (
            "config.py 默认监听 127.0.0.1，镜像里不改的话容器外面进不来"
        )

    def test_timezone_is_pinned(self, dockerfile: str) -> None:
        assert "TZ=Asia/Shanghai" in dockerfile, (
            "预约提醒按 date.today() 算「今天」，UTC 会差一天且不报错"
        )

    def test_the_package_itself_is_not_installed(self, dockerfile: str) -> None:
        """装成 site-packages 里的包，ROOT_DIR 就会推错，两处静默出错。"""
        pip_lines = [line for line in _logical_lines(DOCKERFILE) if "pip install" in line]
        assert pip_lines, "没找到 pip install，这条测试自己失效了"
        for line in pip_lines:
            assert "-r /tmp/requirements.txt" in line, (
                "依赖要从 pyproject.toml 抽出来装，不能 `pip install .`"
            )
            assert "tomllib" in line, "依赖清单得从 pyproject.toml 读，不能另抄一份"
            assert not re.search(r"pip install[^&|]*?\s\.(\s|$)", line)

    def test_the_station_table_lives_outside_the_mount_point(self) -> None:
        """放在 /app/data 下会被卷盖住，等于没放。"""
        assert (ROOT / "lushu" / "seed" / "rail_stations.json").is_file(), (
            "车站名表不在 lushu/seed/ 下，镜像会缺料"
        )

    def test_the_entrypoint_is_made_executable(self, dockerfile: str) -> None:
        """从 Windows 构建时 COPY 过来的文件不一定带执行位，这行是必须的。"""
        assert "chmod +x /usr/local/bin/entrypoint.sh" in dockerfile
        assert ENTRYPOINT.is_file()

    def test_the_entrypoint_hands_over_to_the_command(self) -> None:
        body = ENTRYPOINT.read_text(encoding="utf-8")
        assert 'exec "$@"' in body, "不 exec 的话信号传不到服务进程，docker stop 会干等"

    def test_the_entrypoint_writes_into_the_data_directory(self) -> None:
        body = ENTRYPOINT.read_text(encoding="utf-8")
        assert 'DATA_DIR="${LUSHU_DATA_DIR:-/app/data}"' in body
        assert 'SEED_DIR="/app/lushu/seed"' in body
        assert 'cp "$SEED_DIR/$name" "$DATA_DIR/$name"' in body


class TestTheEntrypointSurvivesLinux:
    def test_it_uses_lf_line_endings(self) -> None:
        """CRLF 的 shell 脚本在 Linux 上起不来。

        `.gitattributes` 里的 `* text=auto eol=lf` 只在签出时管用；构建读的是
        工作区里那份文件，编辑器要是写回 CRLF，报出来的是
        「no such file or directory」，指的却是文件明明在那儿 —— 很难查。
        """
        raw = ENTRYPOINT.read_bytes()
        assert b"\r\n" not in raw, "entrypoint.sh 是 CRLF，容器会以「找不到文件」起不来"
        assert raw.startswith(b"#!/bin/sh\n"), "首行就得是 shebang，后面直接换行"


class TestTheHealthcheck:
    def test_it_probes_a_route_that_actually_exists(self, dockerfile: str) -> None:
        probed = re.search(r"http://127\.0\.0\.1:'\+[^+]*\+'([^']+)'", dockerfile)
        assert probed, "没解析出健康检查探的路径，这条测试自己失效了"
        path = probed.group(1)

        from lushu.app import create_app

        # 这一版 FastAPI 把 include_router 的结果收成 _IncludedRouter 放在
        # app.routes 上，那上面的 path 一律是 None —— 认路径要看 openapi()。
        served = set(create_app().openapi().get("paths", {}))
        assert path in served, f"健康检查探 {path}，但应用没有这个路由，容器会永远 unhealthy"

    def test_it_follows_the_configured_port(self, dockerfile: str) -> None:
        assert "os.environ.get('LUSHU_PORT','8756')" in dockerfile, (
            "端口写死的话，改了 LUSHU_PORT 之后健康检查会连错端口"
        )


class TestThePortIsOneNumberInThreePlaces:
    def test_dockerfile_agrees_with_itself(self, dockerfile: str) -> None:
        env_port = re.search(r"LUSHU_PORT=(\d+)", dockerfile)
        exposed = re.search(r"^EXPOSE\s+(\d+)", dockerfile, flags=re.MULTILINE)
        assert env_port and exposed
        assert env_port.group(1) == exposed.group(1)

    def test_compose_maps_onto_the_image_port(self, dockerfile: str) -> None:
        env_port = re.search(r"LUSHU_PORT=(\d+)", dockerfile)
        mapping = re.search(r'-\s*"(\d+):(\d+)"', COMPOSE.read_text(encoding="utf-8"))
        assert env_port and mapping
        assert mapping.group(2) == env_port.group(1), (
            f"compose 把宿主机端口映到容器 {mapping.group(2)}，"
            f"而镜像里服务监听 {env_port.group(1)}"
        )


def _copy_sources(dockerfile: str) -> list[str]:
    """Dockerfile 里从构建上下文拷的源路径（不含 --from 那几条）。"""
    sources: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from=" in stripped:
            continue
        parts = stripped.split()[1:]
        sources.extend(parts[:-1])  # 最后一个是目的路径
    return sources


class TestEverythingCopiedExists:
    def test_copy_sources_are_present_in_the_context(self, dockerfile: str) -> None:
        sources = _copy_sources(dockerfile)
        assert sources, "没解析到任何 COPY，这条测试自己失效了"
        missing = [name for name in sources if not (ROOT / name).exists()]
        assert not missing, f"构建上下文里没有这些路径：{missing}"

    def test_copy_sources_are_all_under_version_control(self, dockerfile: str) -> None:
        """构建要用的东西必须在版本库里。

        GitHub Actions 是从 clone 出来的仓库构建的，工作区里只有被跟踪的文件。
        一个文件要是被 .gitignore 挡着，本机构建一切正常、CI 上却以「文件不存在」
        失败 —— 车站名表就踩过这个坑，现在挪进了 `lushu/seed/`。

        这里直接问 git 自己，不另写一份忽略规则：规则只能有一处，
        多写一份迟早两处不一致。
        """
        ignored = [
            name
            for name in _copy_sources(dockerfile)
            if subprocess.run(
                ["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True
            ).returncode
            == 0
        ]
        assert not ignored, f"这些构建要用的路径被 .gitignore 挡着，CI 上会缺：{ignored}"


class TestComposeKeepsDataOnTheNas:
    def test_the_data_directory_is_the_mount_point(self) -> None:
        body = COMPOSE.read_text(encoding="utf-8")
        assert ":/app/data" in body, "库要落在卷上，否则删容器就没了"

    def test_it_restarts_and_rotates_logs(self) -> None:
        body = COMPOSE.read_text(encoding="utf-8")
        assert "restart: unless-stopped" in body
        assert "max-size" in body, "NAS 上日志不限长会一直堆下去"


class TestTheImageNameIsOneStringInSeveralPlaces:
    """镜像名分布在三个文件里，对不上就是「拉不到镜像」这种一眼看不出原因的事。

    以 workflow 为准：真正决定推到哪个仓库的是它，其余几处跟着它走。
    """

    def test_compose_and_docs_use_the_name_the_workflow_pushes(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
        pushed = re.search(r"^\s*images:\s*(\S+)", workflow, flags=re.MULTILINE)
        assert pushed, "workflow 里没解析出 images:，这条测试自己失效了"
        name = pushed.group(1)

        assert f"image: {name}:" in COMPOSE.read_text(encoding="utf-8"), (
            f"compose 拉的不是 workflow 推的那个名字（{name}）"
        )
        for path in (ROOT / "README.md", ROOT / "docs" / "DEPLOY.md"):
            assert name in path.read_text(encoding="utf-8"), f"{path.name} 里的镜像名不是 {name}"
