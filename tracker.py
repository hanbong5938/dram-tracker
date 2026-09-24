"""Command-line entry point for the local DRAM price tracker."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence


DATA_DIR = Path.home() / "Library" / "Application Support" / "DRAMTracker"
DEFAULT_DB = DATA_DIR / "prices.sqlite3"
DEFAULT_CHART = DATA_DIR / "index.html"
DEFAULT_PUBLIC_CHART = DATA_DIR / "public" / "index.html"
DEFAULT_PUBLICATION_CONFIG = DATA_DIR / "publication.json"
_REPOSITORY_COMPONENT = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?"
_GITHUB_REPOSITORY_RE = re.compile(
    r"^%s/%s$" % (_REPOSITORY_COMPONENT, _REPOSITORY_COMPONENT)
)
_GITHUB_REMOTE_RE = re.compile(r"^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$")
DEFAULT_CSV = DATA_DIR / "prices.csv"
DEFAULT_BACKUP = DATA_DIR / "backup.sqlite3"


def _absolute(path: Path) -> Path:
    return path.expanduser().resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker.py",
        description=(
            "개인 DRAM 가격 추적기: 공개 홈페이지의 현재 표시값과 사용자가 제공한 "
            "기록만 로컬에 저장합니다. 유료 이력 구독이나 투자 기능은 제공하지 않습니다."
        ),
        epilog=(
            "데이터 디렉터리와 publication.json은 로컬에만 생성됩니다. 공개 배포에는 "
            "별도로 GitHub 저장소를 만들고 Pages를 설정해야 하며, 이 도구는 계정을 변경하지 "
            "않습니다. 출처 이용약관과 게시 권한은 사용자가 직접 확인해야 합니다."
        ),
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=str(DEFAULT_DB),
        help="SQLite 파일 (기본값: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    history = commands.add_parser(
        "import-history",
        help="제공된 과거 기록 CSV를 가져옵니다 (유료 이력 서비스에 연결하지 않음)",
        description="공개/사용자 제공 기록을 로컬 SQLite에 한 번 가져옵니다.",
    )
    history.add_argument("--csv", metavar="PATH", help="가져올 CSV (생략하면 history.py 옆 seed.csv)")

    collect = commands.add_parser(
        "collect",
        help="공개 DRAM Exchange 홈페이지에서 오늘의 중간 세션 값을 한 번 수집합니다",
        description="자동 재시도 없이 공개 홈페이지를 한 번 읽고 검증합니다.",
    )
    collect.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="기대하는 대만(+08) 출처 날짜 (생략하면 현재 대만 날짜)",
    )

    chart = commands.add_parser("chart", help="비공개 또는 게시용 HTML 그래프를 생성합니다")
    chart.add_argument("--public", action="store_true", help="개인 메타데이터를 제외한 게시용 그래프를 생성합니다")
    chart.add_argument(
        "--source-kinds", nargs="+", choices=("official", "community"),
        default=("official", "community"), help="게시용 그래프에 포함할 출처 그룹",
    )
    chart.add_argument(
        "--output", metavar="PATH",
        help="HTML 출력 경로 (비공개 기본값: %s, 게시용 기본값: %s)"
        % (DEFAULT_CHART, DEFAULT_PUBLIC_CHART),
    )

    export_csv = commands.add_parser("export-csv", help="저장된 관측값을 CSV로 내보냅니다")
    export_csv.add_argument("--output", metavar="PATH", help="CSV 출력 경로 (기본값: %(default)s)", default=str(DEFAULT_CSV))

    backup = commands.add_parser(
        "backup",
        help="SQLite 온라인 백업을 생성합니다 (복원에 사용할 수 있음)",
    )
    backup.add_argument(
        "--output",
        metavar="PATH",
        help="백업 SQLite 경로 (기본값: %(default)s)",
        default=str(DEFAULT_BACKUP),
    )

    run = commands.add_parser(
        "run",
        help="수집 후 로컬 HTML 그래프를 생성합니다",
        description="수집에 실패해도 기존/부분 데이터로 그래프 생성을 시도합니다.",
    )
    run.add_argument("--output", metavar="PATH", help="HTML 출력 경로 (기본값: %(default)s)", default=str(DEFAULT_CHART))
    run.add_argument("--publish-config", metavar="PATH", help="수집 성공 시 사용할 게시 설정 JSON")

    schedule = commands.add_parser(
        "schedule",
        help="명시적으로 macOS launchd 예약 등록을 설치하거나 제거합니다 (자동 설치하지 않음)",
        description=(
            "예약 등록/제거는 사용자의 명시적 동작입니다. --install에는 "
            "--accept-source-terms가 필요하며, 확인은 권리를 부여하지 않습니다."
        ),
    )
    schedule_action = schedule.add_mutually_exclusive_group(required=True)
    schedule_action.add_argument("--install", action="store_true", help="launchd 예약 등록을 설치합니다")
    schedule_action.add_argument("--uninstall", action="store_true", help="launchd 예약 등록을 제거합니다")
    schedule.add_argument(
        "--output",
        metavar="PATH",
        help="예약 실행 시 생성할 HTML 경로 (기본값: %(default)s)",
        default=str(DEFAULT_CHART),
    )
    schedule.add_argument(
        "--accept-source-terms",
        action="store_true",
        help="출처 이용약관을 확인했음을 명시합니다 (권리/허가를 부여하지 않음)",
    )
    schedule.add_argument("--publish-config", metavar="PATH", help="예약 실행 시 사용할 게시 설정 JSON")

    configure = commands.add_parser(
        "configure-publish",
        help="GitHub Pages 게시 설정 JSON을 로컬에 준비합니다 (저장소/Pages는 별도 생성)",
        description=(
            "GitHub 저장소 생성이나 Pages 설정, 계정 변경 없이 로컬 JSON만 만듭니다. "
            "기본 설정은 게시 비활성 상태입니다."
        ),
    )
    configure.add_argument("--repository", required=True, metavar="OWNER/REPO")
    configure.add_argument("--ssh-key", metavar="PATH", help="게시 전용 SSH 개인 키")
    configure.add_argument(
        "--source-kinds", nargs="+", choices=("official", "community"),
        default=("official", "community"), help="공개할 출처 그룹",
    )
    configure.add_argument(
        "--output", metavar="PATH", default=str(DEFAULT_PUBLICATION_CONFIG),
        help="설정 JSON 경로 (기본값: %(default)s)",
    )
    configure.add_argument(
        "--accept-publication-terms", action="store_true",
        help="게시 권한 책임을 확인하고 설정을 활성화합니다 (권리를 부여하지 않음)",
    )
    configure.add_argument("--replace", action="store_true", help="기존 설정 파일을 원자적으로 교체합니다")

    publish = commands.add_parser("publish", help="현재 저장 데이터로 게시용 HTML을 만들고 게시합니다")
    publish.add_argument("--config", required=True, metavar="PATH")

    rollback = commands.add_parser("rollback", help="이전 공개 커밋의 내용으로 새 복원 커밋을 게시합니다")
    rollback.add_argument("--config", required=True, metavar="PATH")
    rollback.add_argument("--commit", required=True, metavar="FULLSHA")

    publication_status = commands.add_parser(
        "publication-status", help="최근 게시 실행 상태를 표시합니다"
    )
    publication_status.add_argument("--limit", type=int, default=20, metavar="N")
    return parser

def _configure_publish(args: argparse.Namespace) -> int:
    repository = args.repository.strip()
    if not _GITHUB_REPOSITORY_RE.fullmatch(repository):
        raise ValueError("--repository는 안전한 OWNER/REPO 형식이어야 합니다.")
    output = _absolute(Path(args.output))
    if output.exists() and not args.replace:
        raise FileExistsError("게시 설정이 이미 있습니다. 교체하려면 --replace를 사용하십시오: %s" % output)
    if output.is_symlink():
        raise ValueError("심볼릭 링크에는 게시 설정을 쓸 수 없습니다: %s" % output)

    key: Optional[Path] = _absolute(Path(args.ssh_key)) if args.ssh_key else None
    if args.accept_publication_terms:
        if key is None:
            raise ValueError("GitHub 게시를 활성화하려면 --ssh-key로 게시 전용 키를 지정해야 합니다.")
        _require_safe_key(key)
    elif key is not None:
        _require_safe_key(key)

    owner, repo = repository.split("/", 1)
    payload = {
        "remote_url": "git@github.com:%s/%s.git" % (owner, repo),
        "branch": "main",
        "public_url": "https://%s.github.io/%s/" % (owner, repo),
        "work_dir": str(output.parent / "publisher-work"),
        "ssh_key": str(key) if key is not None else None,
        "publication_authorized": bool(args.accept_publication_terms),
        "source_kinds": list(dict.fromkeys(args.source_kinds)),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % output.name, suffix=".tmp", dir=str(output.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(output))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    state = "활성" if payload["publication_authorized"] else "비활성"
    print("게시 설정을 %s 상태로 만들었습니다: %s" % (state, output))
    print("저장소 생성과 GitHub Pages 설정은 외부에서 별도로 완료해야 합니다.")
    return 0


def _require_safe_key(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ValueError("게시 전용 SSH 키를 읽을 수 없습니다: %s" % exc) from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError("게시 전용 SSH 키는 일반 파일이어야 합니다: %s" % path)
    if details.st_mode & 0o077:
        raise ValueError("게시 전용 SSH 키 권한은 그룹/기타 사용자가 읽을 수 없게 설정해야 합니다: %s" % path)


def _load_publish_config(path: Path) -> Dict[str, Any]:
    import publisher

    config = publisher.load_config(_absolute(path))
    if not config["publication_authorized"]:
        raise publisher.PublishError(
            "게시 설정이 비활성 상태입니다. 권한을 확인한 뒤 publication_authorized를 활성화하십시오."
        )
    if _GITHUB_REMOTE_RE.fullmatch(config["remote_url"]):
        key = config.get("ssh_key")
        if key is None:
            raise publisher.PublishError("GitHub 게시에는 게시 전용 ssh_key가 필요합니다.")
        try:
            _require_safe_key(Path(key))
        except ValueError as exc:
            raise publisher.PublishError(str(exc)) from exc
    return config


@contextlib.contextmanager
def _operation_lock(database_path: Path) -> Iterator[bool]:
    lock_path = database_path.with_name(database_path.name + ".operation.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def _latest_collection_run_id(connection: Any) -> Optional[int]:
    row = connection.execute("SELECT MAX(run_id) AS run_id FROM collection_runs").fetchone()
    return int(row["run_id"]) if row is not None and row["run_id"] is not None else None


def _finish_publication(connection: Any, run_id: int, result: Mapping[str, Any]) -> int:
    import store

    status = str(result["status"])
    store.finish_publication_run(
        connection,
        run_id,
        status=status,
        data_digest=result.get("data_digest"),
        commit_sha=result.get("commit_sha"),
        error_text=result.get("error_text"),
    )
    if status == "pending":
        print("게시 전송은 완료되었지만 공개 URL 반영을 기다리는 중입니다.", file=sys.stderr)
        return 2
    print("게시 완료 상태: %s (%s)" % (status, result.get("public_url", "")))
    return 0


def _record_publication_failure(
    connection: Any,
    run_id: int,
    status: str,
    error: BaseException,
) -> None:
    import store

    store.finish_publication_run(connection, run_id, status=status, error_text=str(error))

def _close(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        pass


def _command(args: argparse.Namespace) -> int:
    database_path = _absolute(Path(args.db))
    if args.command not in {"collect", "run", "publish", "rollback"}:
        return _execute(args, database_path)
    with _operation_lock(database_path) as acquired:
        if not acquired:
            print("다른 수집/게시 작업이 실행 중이어서 이번 작업을 건너뜁니다.")
            return 0
        return _execute(args, database_path)


def _execute(args: argparse.Namespace, database_path: Path) -> int:
    command = args.command

    if command == "configure-publish":
        return _configure_publish(args)

    if command == "schedule":
        import schedule

        chart_output_path = _absolute(Path(args.output))
        publish_config = (
            str(_absolute(Path(args.publish_config))) if args.publish_config else None
        )
        if args.install:
            if not args.accept_source_terms:
                raise ValueError(
                    "설치하려면 --accept-source-terms를 명시해야 합니다. "
                    "확인은 출처 이용 권리나 허가를 부여하지 않습니다."
                )
            result = schedule.install(
                str(_absolute(Path(sys.executable))),
                str(_absolute(Path(__file__))),
                str(database_path),
                str(chart_output_path),
                accept_source_terms=True,
                publish_config=publish_config,
            )
            print("launchd 예약 등록을 설치했습니다: %s" % result)
            return 0
        schedule.uninstall()
        print("launchd 예약 등록을 제거했습니다.")
        return 0

    # Imports are intentionally command-local: --help and unrelated commands
    # must not require optional sibling modules to be importable.
    import store

    connection = store.connect(database_path)
    try:
        if command == "import-history":
            import history

            csv_path = _absolute(Path(args.csv)) if args.csv else None
            inserted = history.import_history(connection, csv_path)
            print("과거 기록 %d건을 가져왔습니다." % inserted)
            return 0

        if command == "collect":
            import collect

            expected_date = args.date or collect.taipei_today()
            observation = collect.collect_once(connection, expected_date)
            print(
                "수집 완료: %s 중간 세션 %s"
                % (observation["observed_date"], observation["value_text"])
            )
            return 0

        if command == "chart":
            import chart

            default_output = DEFAULT_PUBLIC_CHART if args.public else DEFAULT_CHART
            output_path = _absolute(Path(args.output or default_output))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if args.public:
                result = chart.render_public(
                    connection,
                    output_path,
                    source_kinds=tuple(dict.fromkeys(args.source_kinds)),
                )
                print("게시용 그래프를 생성했습니다: %s" % result)
            else:
                result = chart.render(connection, output_path)
                print("그래프를 생성했습니다: %s" % result)
            return 0

        if command == "export-csv":
            import chart

            output_path = _absolute(Path(args.output))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            result = chart.export_csv(connection, output_path)
            print("CSV를 내보냈습니다: %s" % result)
            return 0

        if command == "backup":
            output_path = _absolute(Path(args.output))
            result = store.backup_database(connection, output_path)
            print("SQLite 백업을 생성했습니다: %s" % result)
            return 0

        if command == "publication-status":
            if args.limit < 1:
                raise ValueError("--limit는 1 이상이어야 합니다.")
            rows = store.publication_runs(connection, limit=args.limit)
            if not rows:
                print("게시 실행 기록이 없습니다.")
                return 0
            for row in rows:
                detail = row["error_text"] or row["public_url"] or ""
                linked = (
                    " collection=%s" % row["collection_run_id"]
                    if row["collection_run_id"] is not None
                    else ""
                )
                print(
                    "%s %s%s%s"
                    % (
                        row["started_at"],
                        row["status"],
                        linked,
                        " - %s" % detail if detail else "",
                    )
                )
            return 0

        if command == "publish":
            return _manual_publish(connection, _absolute(Path(args.config)))

        if command == "rollback":
            return _manual_rollback(
                connection, _absolute(Path(args.config)), args.commit
            )

        if command == "run":
            return _run(connection, args)

        raise ValueError("알 수 없는 명령입니다: %s" % command)
    finally:
        _close(connection)


def _manual_publish(connection: Any, config_path: Path) -> int:
    import chart
    import publisher
    import store

    try:
        config = _load_publish_config(config_path)
    except Exception as exc:
        run_id = store.start_publication_run(connection)
        _record_publication_failure(connection, run_id, "blocked", exc)
        raise
    run_id = store.start_publication_run(
        connection, public_url=config["public_url"]
    )
    candidate = DEFAULT_PUBLIC_CHART
    candidate.parent.mkdir(parents=True, exist_ok=True)
    try:
        chart.render_public(
            connection, candidate, source_kinds=tuple(config["source_kinds"])
        )
        result = publisher.publish(candidate, config)
    except Exception as exc:
        _record_publication_failure(connection, run_id, "error", exc)
        raise
    return _finish_publication(connection, run_id, result)


def _manual_rollback(connection: Any, config_path: Path, commit_sha: str) -> int:
    import publisher
    import store

    try:
        config = _load_publish_config(config_path)
    except Exception as exc:
        run_id = store.start_publication_run(connection)
        _record_publication_failure(connection, run_id, "blocked", exc)
        raise
    run_id = store.start_publication_run(
        connection, public_url=config["public_url"]
    )
    try:
        result = publisher.rollback(config, commit_sha)
    except Exception as exc:
        _record_publication_failure(connection, run_id, "error", exc)
        raise
    return _finish_publication(connection, run_id, result)


def _run(connection: Any, args: argparse.Namespace) -> int:
    import chart
    import collect
    import publisher
    import store

    output_path = _absolute(Path(args.output))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    expected_date = collect.taipei_today()
    collection_error: Optional[BaseException] = None
    previous_collection_run_id = _latest_collection_run_id(connection)
    try:
        observation = collect.collect_once(connection, expected_date)
        print(
            "수집 완료: %s 중간 세션 %s"
            % (observation["observed_date"], observation["value_text"])
        )
    except Exception as exc:
        collection_error = exc
        print("수집 실패: %s" % exc, file=sys.stderr)
    latest_collection_run_id = _latest_collection_run_id(connection)
    collection_run_id = (
        latest_collection_run_id
        if latest_collection_run_id != previous_collection_run_id
        else None
    )

    publication_run_id: Optional[int] = None
    config: Optional[Dict[str, Any]] = None
    config_error: Optional[BaseException] = None
    if args.publish_config:
        try:
            config = _load_publish_config(_absolute(Path(args.publish_config)))
        except Exception as exc:
            config_error = exc
        publication_run_id = store.start_publication_run(
            connection,
            collection_run_id=collection_run_id,
            public_url=config["public_url"] if config is not None else None,
        )
        if config_error is not None:
            _record_publication_failure(
                connection, publication_run_id, "blocked", config_error
            )
            publication_run_id = None
            print("게시 차단: %s" % config_error, file=sys.stderr)
        elif collection_error is not None:
            _record_publication_failure(
                connection, publication_run_id, "blocked", collection_error
            )
            publication_run_id = None


    try:
        result = chart.render(connection, output_path)
        print("그래프를 생성했습니다: %s" % result)
    except Exception as chart_error:
        if publication_run_id is not None:
            _record_publication_failure(
                connection, publication_run_id, "error", chart_error
            )
        if collection_error is not None:
            raise RuntimeError(
                "수집과 그래프 생성이 모두 실패했습니다: %s; %s"
                % (collection_error, chart_error)
            ) from chart_error
        raise
    if config_error is not None and collection_error is None:
        raise config_error




    if publication_run_id is not None and config is not None:
        candidate = DEFAULT_PUBLIC_CHART
        candidate.parent.mkdir(parents=True, exist_ok=True)
        try:
            chart.render_public(
                connection, candidate, source_kinds=tuple(config["source_kinds"])
            )
            publish_result = publisher.publish(candidate, config)
        except Exception as exc:
            _record_publication_failure(
                connection, publication_run_id, "error", exc
            )
            raise
        publication_code = _finish_publication(
            connection, publication_run_id, publish_result
        )
        if publication_code:
            return publication_code
    return 1 if collection_error is not None else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _command(args)
    except KeyboardInterrupt:
        print("사용자가 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        print("오류: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
