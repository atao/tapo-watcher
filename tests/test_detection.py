IP = "192.168.1.50"
MAC = "aa:bb:cc:dd:ee:01"


def test_connected_when_ping_answers(tp, net):
    net.ping[IP] = True
    net.neigh[IP] = f"dev eth0 lladdr {MAC} REACHABLE"
    assert tp.phone_is_connected(IP, MAC)


def test_connected_when_ping_ignored_but_arp_fresh(tp, net):
    # Sleeping iPhone: ignores ICMP but answers ARP
    net.neigh[IP] = f"dev eth0 lladdr {MAC} REACHABLE"
    assert tp.phone_is_connected(IP, MAC)


def test_away_when_arp_stale_or_failed(tp, net):
    net.neigh[IP] = f"dev eth0 lladdr {MAC} STALE"
    assert not tp.phone_is_connected(IP, MAC)
    net.neigh[IP] = "dev eth0 FAILED"
    assert not tp.phone_is_connected(IP, MAC)


def test_away_when_ip_belongs_to_another_mac(tp, net):
    net.ping[IP] = True
    net.neigh[IP] = "dev eth0 lladdr 00:11:22:33:44:55 REACHABLE"
    assert not tp.phone_is_connected(IP, MAC)


def test_ip_from_mac_uses_arp_table_without_sweep(tp, net):
    net.neigh["192.168.1.77"] = f"dev eth0 lladdr {MAC} STALE"
    assert tp.ip_from_mac(MAC) == "192.168.1.77"
    assert not net.swept


def test_ip_from_mac_sweeps_when_unknown(tp, net):
    assert tp.ip_from_mac(MAC) == ""
    assert net.swept


def test_ip_from_mac_ignores_failed_entries(tp, net):
    net.neigh["192.168.1.77"] = f"dev eth0 lladdr {MAC} FAILED"
    assert tp.ip_from_mac(MAC) == ""


def test_phone_seen_follows_ip_change(tp, net):
    new_ip = "192.168.1.88"
    net.ping[new_ip] = True
    net.neigh[new_ip] = f"dev eth0 lladdr {MAC} REACHABLE"

    assert tp.phone_seen(IP, MAC)
    assert tp._known_ip[MAC] == new_ip
    # Next check goes straight to the new IP
    assert tp.phone_seen(IP, MAC)


def test_phone_seen_without_mac_does_not_search(tp, net):
    assert not tp.phone_seen(IP, "")
    assert not net.swept


def test_any_phone_connected(tp, net, monkeypatch):
    monkeypatch.setattr(tp, "PHONES", [(IP, MAC), ("192.168.1.51", "")])
    assert not tp.any_phone_connected()
    net.ping["192.168.1.51"] = True
    assert tp.any_phone_connected()


def test_ip_from_mac_sweeps_each_camera_subnet_once(tp, net, monkeypatch):
    monkeypatch.setattr(
        tp,
        "CAMERAS",
        [
            tp.Camera("a", "192.168.1.100", "u", "p", "none", "none"),
            tp.Camera("b", "192.168.1.101", "u", "p", "none", "none"),
            tp.Camera("c", "10.0.0.5", "u", "p", "none", "none"),
        ],
    )
    pinged = []

    class Popen:
        def __init__(self, cmd, **kwargs):
            pinged.append(cmd[-1])

        def wait(self):
            return 0

    monkeypatch.setattr(tp.subprocess, "Popen", Popen)
    tp.ip_from_mac(MAC)
    assert len(pinged) == 2 * 254
    assert {ip.rsplit(".", 1)[0] for ip in pinged} == {"192.168.1", "10.0.0"}


def test_ip_from_mac_sweep_limits_concurrent_pings(tp, net, monkeypatch):
    running, peak = [0], [0]

    class Popen:
        def __init__(self, cmd, **kwargs):
            running[0] += 1
            peak[0] = max(peak[0], running[0])

        def wait(self):
            running[0] -= 1
            return 0

    monkeypatch.setattr(tp.subprocess, "Popen", Popen)
    tp.ip_from_mac(MAC)
    assert peak[0] <= tp.SWEEP_BATCH


def test_ip_from_mac_survives_fork_failure(tp, net, monkeypatch):
    calls = [0]

    class Popen:
        def __init__(self, cmd, **kwargs):
            calls[0] += 1
            if calls[0] % 10 == 0:
                raise BlockingIOError(11, "Resource temporarily unavailable")

        def wait(self):
            return 0

    monkeypatch.setattr(tp.subprocess, "Popen", Popen)
    assert tp.ip_from_mac(MAC) == ""
