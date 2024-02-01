import datetime
import time
from typing import Any, Callable

from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    after_log,
)

from infra_client.gcp import constats as const
import logger

log = logger.get_logger(__name__)


class RetryException(Exception):
    """Exception to indicate tenacity.retry that should try again"""


@retry(
    # HttpError 429 when requesting https://iam.googleapis.com/v1/projects/pcaas-dev-364815/serviceAccounts?alt=json returned
    # "A quota has been reached for project number 871271722260: Service accounts created per minute per project.
    retry=retry_if_exception_type(RetryException),
    stop=stop_after_attempt(10),
    wait=wait_exponential(multiplier=1, min=15, max=90),
    after=after_log(log, logger.logging.WARNING),
)
def _get_status_retriable(get_status: Callable) -> Any:
    try:
        return get_status()
    except HttpError as exc:
        if exc.status_code == 429:
            log.warning(
                f"Too many request received from Goggle API when getting status {get_status.__name__}"
            )
            raise RetryException from exc
        raise exc


def wait(
    msg: str,
    get_status: Callable,
    stop_condition: Callable,
    timeout_secs: int,
    sleep_secs: int = 5,
    print_status: bool = True,
) -> None:
    """helper to wait until timeour"""
    log.info(f"Waiting for {msg}")
    timeout = (start := time.time()) + timeout_secs

    def status_str(status: Any) -> str:
        return f": {status=}" if print_status else ""

    while True:
        status = None
        if stop_condition(status := _get_status_retriable(get_status)):
            log.info(f"{msg} successful: {status_str(status)}")
            break
        if time.time() > timeout:
            log.error(
                msg := f"After {timeout_secs} secs, still waiting for {msg}: {status_str(status)}"
            )
            raise TimeoutError(msg)
        elapsed = datetime.timedelta(seconds=time.time() - start)
        log.warning(f"{elapsed}... Still waiting for {msg}: {status_str(status)}")
        time.sleep(sleep_secs)


def get_gke_region_country_code(gke_region: str) -> str:
    """Define the mapping between regions and country codes"""
    return const.GKE_REGIONS_TO_COUNTRY_CODE[gke_region]
