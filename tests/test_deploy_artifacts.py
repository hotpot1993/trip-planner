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
        assert "data/*" in ignore_lines, "data/ 下的运行期数据没有被整目录排掉"

    def test_no_runtime_data_is_re_included(self, ignore_lines: list[str]) -> None:
        re_included = [line for line in ignore_lines if line.startswith("!data/")]
        assert re_included == ["!data/rail_stations.json"], (
            f"data/ 下只该放行车站名表，现在是 {re_included}"
        )

    def test_the_station_table_is_allowed_back_in_with_the_right_pattern(
        self, ignore_lines: list[str]
    ) -> None:
        """必须是 `data/*` 再接 `!data/rail_stations.json`，不能写 `data/`。

        父目录被排除之后，`!` 再想放行它里面的文件是无效的 —— 写成 `data/`
        的话这条放行会静默失效，然后构建以「文件不存在」失败。
        """
        assert "data/*" in ignore_lines
        assert ignore_lines.index("data/*") < ignore_lines.index("!data/rail_stations.json")
        assert "data/" not in ignore_lines

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

    def test_the_station_table_is_seeded_outside_the_mount_point(self, dockerfile: str) -> None:
        """放 /app/data 会被卷盖住，等于没放。"""
        assert "COPY data/rail_stations.json ./seed/rail_stations.json" in dockerfile

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
        assert "/app/seed/" in body


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


class TestEverythingCopiedExists:
    def test_copy_sources_are_present_in_the_context(self, dockerfile: str) -> None:
        sources: list[str] = []
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if not stripped.startswith("COPY ") or "--from=" in stripped:
                continue
            parts = stripped.split()[1:]
            sources.extend(parts[:-1])  # 最后一个是目的路径

        assert sources, "没解析到任何 COPY，这条测试自己失效了"

        missing = [name for name in sources if not (ROOT / name).exists()]
        if missing == ["data/rail_stations.json"]:
            pytest.skip(
                "data/rail_stations.json 不在版本库里（.gitignore 的 data/ 排掉了），"
                "在 NAS 上从 clone 出来的仓库构建时也会缺它 —— 见 docs/DEPLOY.md"
            )
        assert not missing, f"构建上下文里没有这些路径：{missing}"


class TestComposeKeepsDataOnTheNas:
    def test_the_data_directory_is_the_mount_point(self) -> None:
        body = COMPOSE.read_text(encoding="utf-8")
        assert ":/app/data" in body, "库要落在卷上，否则删容器就没了"

    def test_it_restarts_and_rotates_logs(self) -> None:
        body = COMPOSE.read_text(encoding="utf-8")
        assert "restart: unless-stopped" in body
        assert "max-size" in body, "NAS 上日志不限长会一直堆下去"
