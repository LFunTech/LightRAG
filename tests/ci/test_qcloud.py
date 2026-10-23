import pytest


def test_qcloud_checks_exact_https_rule_and_all_backend_health():
    from lightrag_delivery.qcloud import verify_backends

    config = {
        "load_balancer_id": "lb-a",
        "listener_id": "lbl-a",
        "location_id": "loc-a",
    }
    targets = {
        "Listeners": [
            {
                "ListenerId": "lbl-a",
                "Protocol": "HTTPS",
                "Rules": [
                    {
                        "LocationId": "loc-a",
                        "Targets": [
                            {
                                "PrivateIpAddresses": ["10.1.0.1"],
                                "Port": 9621,
                                "Weight": 10,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    health = {
        "LoadBalancers": [
            {
                "LoadBalancerId": "lb-a",
                "Listeners": [
                    {
                        "ListenerId": "lbl-a",
                        "Rules": [
                            {
                                "LocationId": "loc-a",
                                "Targets": [
                                    {
                                        "IP": "10.1.0.1",
                                        "Port": 9621,
                                        "HealthStatus": True,
                                        "Weight": 10,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assert verify_backends(
        config, targets, health, expected_ips={"10.1.0.1"}, port=9621
    )
    with pytest.raises(ValueError):
        verify_backends(
            config, targets, health, expected_ips={"10.1.0.1", "10.1.0.2"}, port=9621
        )
    health["LoadBalancers"][0]["Listeners"][0]["Rules"][0]["Targets"][0][
        "HealthStatus"
    ] = False
    with pytest.raises(ValueError):
        verify_backends(config, targets, health, expected_ips={"10.1.0.1"}, port=9621)


def test_read_only_cloud_client_never_calls_mutation_or_redirect():
    from lightrag_delivery.qcloud import QCloudObserver

    observer = QCloudObserver(
        region="ap-guangzhou", secret_id="synthetic", secret_key="synthetic"
    )
    with pytest.raises(ValueError):
        observer.call("DeregisterTargets", {})
