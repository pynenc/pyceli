from kubernetes.client.exceptions import ApiException

from piceli.k8s.exceptions import api_exceptions


def test_api_exception_parser() -> None:
    api = ApiException()
    api.status = 404
    api.reason = "NotFound"
    api.message = "Service not found"
    api.body = '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"cronjobs.batch \\"cronjob-name\\" not found","reason":"NotFound","details":{"name":"cronjob-name","group":"batch","kind":"cronjobs"},"code":404}\n'

    parsed = api_exceptions.ApiOperationException.from_api_exception(api)
    assert parsed.code == 404
    assert parsed.status == "Failure"
    assert parsed.reason == "NotFound"
    assert parsed.message == 'cronjobs.batch "cronjob-name" not found'
    assert parsed.not_found is True
    assert parsed.already_exists is False
