"""リトライユーティリティ。

エージェント障害発生時のリトライロジックと安全状態移行を提供する。

使い方:
    from src.utils.retry import retry_with_backoff, RetryExhausted

    @retry_with_backoff(max_retries=3, base_delay=2.0, operation_name="データ収集")
    def fetch_data():
        ...

    # または直接呼び出し:
    result = retry_with_backoff(
        fn=fetch_data,
        args=[arg1],
        max_retries=3,
        base_delay=2.0,
        operation_name="データ収集",
    )
"""

import time
import functools
from typing import Any, Callable, Type

from src.utils.logger import setup_logger

logger = setup_logger("retry")


class RetryExhausted(Exception):
    """リトライ上限に達した際に送出される例外。"""

    def __init__(self, operation: str, attempts: int, last_error: Exception):
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"{operation}: {attempts}回リトライしても失敗。最終エラー: {last_error}"
        )


def retry_with_backoff(
    fn: Callable | None = None,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
    operation_name: str = "処理",
) -> Any:
    """指数バックオフ付きリトライでfnを実行する。

    デコレータとしても、直接呼び出しとしても使用可能。

    Args:
        fn: リトライ対象の関数。None の場合はデコレータとして機能する。
        max_retries: 最大リトライ回数 (初回実行を含まない)。
        base_delay: 初回リトライ前の待機秒数。
        backoff_factor: 待機時間の倍率 (指数バックオフ)。
        max_delay: 最大待機秒数。
        exceptions: リトライ対象とする例外タプル。
        operation_name: ログ用の処理名。

    Returns:
        fnの戻り値。

    Raises:
        RetryExhausted: max_retries回リトライしても成功しなかった場合。
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _execute_with_retry(
                func, args, kwargs,
                max_retries=max_retries,
                base_delay=base_delay,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
                exceptions=exceptions,
                operation_name=operation_name,
            )
        return wrapper

    if fn is not None:
        # 直接呼び出し: retry_with_backoff(fn=func, ...)
        return decorator(fn)
    # デコレータとして使用: @retry_with_backoff(max_retries=3, ...)
    return decorator


def _execute_with_retry(
    fn: Callable,
    args: tuple,
    kwargs: dict,
    *,
    max_retries: int,
    base_delay: float,
    backoff_factor: float,
    max_delay: float,
    exceptions: tuple[Type[Exception], ...],
    operation_name: str,
) -> Any:
    """実際のリトライ実行ロジック。"""
    last_error: Exception | None = None
    delay = base_delay

    for attempt in range(max_retries + 1):
        try:
            result = fn(*args, **kwargs)
            if attempt > 0:
                logger.info(
                    "%s: %d回目の試行で成功", operation_name, attempt + 1
                )
            return result
        except exceptions as e:
            last_error = e
            remaining = max_retries - attempt
            if remaining <= 0:
                logger.error(
                    "%s: %d回全試行失敗。最終エラー: %s",
                    operation_name, max_retries + 1, e,
                )
                raise RetryExhausted(operation_name, max_retries + 1, e) from e

            actual_delay = min(delay, max_delay)
            logger.warning(
                "%s: 試行%d失敗 (%s)。%d秒後にリトライ (残り%d回)...",
                operation_name, attempt + 1, e, int(actual_delay), remaining,
            )
            time.sleep(actual_delay)
            delay *= backoff_factor

    # ここには到達しないが型チェック用
    assert last_error is not None
    raise RetryExhausted(operation_name, max_retries + 1, last_error)


def call_with_retry(
    fn: Callable,
    args: tuple = (),
    kwargs: dict | None = None,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
    operation_name: str = "処理",
) -> Any:
    """fnをリトライ付きで呼び出す (デコレータを使わない場合の代替API)。

    Args:
        fn: リトライ対象の関数。
        args: 位置引数。
        kwargs: キーワード引数。
        max_retries: 最大リトライ回数。
        base_delay: 初回リトライ前の待機秒数。
        backoff_factor: 待機時間の倍率。
        max_delay: 最大待機秒数。
        exceptions: リトライ対象とする例外タプル。
        operation_name: ログ用の処理名。

    Returns:
        fnの戻り値。

    Raises:
        RetryExhausted: max_retries回リトライしても成功しなかった場合。
    """
    return _execute_with_retry(
        fn, args, kwargs or {},
        max_retries=max_retries,
        base_delay=base_delay,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
        exceptions=exceptions,
        operation_name=operation_name,
    )


def enter_safe_hold(reason: str, notify: bool = True) -> None:
    """安全なホールド状態に移行する。

    signals/signals.json を hold で上書きし、Telegramアラートを発報する。
    リトライ上限超過後の最終手段として呼び出す。

    Args:
        reason: 安全移行の理由 (ログ・通知に使用)。
        notify: Telegram通知を行うか。
    """
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from src.utils.config_loader import get_project_root

    ROOT = get_project_root()
    logger.critical("SAFE_HOLD: %s", reason)

    # signals.json を hold で上書き
    signals_path = ROOT / "signals" / "signals.json"
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    hold_payload = {
        "action_type": "hold",
        "signals": [],
        "market_summary": f"SAFE_HOLD: {reason}",
        "ooda": {
            "observe": "retry exhausted",
            "orient": "安全状態に移行",
            "decide": f"hold (reason: {reason})",
        },
        "safe_hold_at": datetime.now(timezone.utc).isoformat(),
        "safe_hold_reason": reason,
    }
    try:
        signals_path.write_text(
            json.dumps(hold_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("SAFE_HOLD: signals.json を hold で上書きしました")
    except Exception as e:
        logger.error("SAFE_HOLD: signals.json の書き込みに失敗: %s", e)

    # kill_switch.json に warning フラグを立てる
    ks_path = ROOT / "state" / "kill_switch.json"
    try:
        if ks_path.exists():
            ks = json.loads(ks_path.read_text(encoding="utf-8"))
            if not isinstance(ks, dict):
                ks = {}
        else:
            ks = {}
        ks["warning"] = True
        ks["warning_reason"] = f"safe_hold: {reason}"
        ks["warning_at"] = datetime.now(timezone.utc).isoformat()
        ks_path.parent.mkdir(parents=True, exist_ok=True)
        ks_path.write_text(
            json.dumps(ks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("SAFE_HOLD: kill_switch.json に warning フラグを設定しました")
    except Exception as e:
        logger.error("SAFE_HOLD: kill_switch.json の更新に失敗: %s", e)

    # Telegram 通知
    if notify:
        try:
            from src.monitor.telegram_notifier import send_message
            send_message(
                f"*SAFE_HOLD* リトライ上限超過\n"
                f"理由: {reason}\n"
                f"対処: signals.json を hold に設定しました。\n"
                f"確認: ログを確認してください。"
            )
        except Exception as e:
            logger.warning("SAFE_HOLD: Telegram通知に失敗: %s", e)
