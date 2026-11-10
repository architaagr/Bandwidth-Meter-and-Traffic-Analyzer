from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib import hub
import time


class BandwidthController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    POLL_INTERVAL = 1.0
    UDP_IDLE_SECONDS = 3
    H2_IP = "10.0.0.2"
    H3_IP = "10.0.0.3"
    H1_IP = "10.0.0.1"
    UDP_PORT = 5001

    def __init__(self, *args, **kwargs):
        super(BandwidthController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.failover_active = False

        # The primary and backup routes match the verified faulttopo port map.
        self.h2_path = {1: 2, 2: 2, 4: 2, 6: 1}
        self.h3_path = {1: 3, 3: 2, 5: 2, 6: 2}
        self.h2_return = {4: 1, 2: 1, 1: 1}
        self.h3_return = {5: 1, 3: 1, 1: 1}
        self.h2_backup = {3: 2, 5: 2}
        self.h2_backup_return = {6: 4, 5: 1, 3: 1, 1: 1}

        self.previous_flow_counters = {}
        self.latest_udp_flows = {}
        self.switch_udp_previous = {}
        self.switch_udp_pending = {}
        self.switch_udp_session = {}
        self.last_s1_sample_time = None

        # One transfer session is measured only at s1 (the ingress switch).
        # Counting the same packet at s1, s2, s4 and s6 would multiply-count it.
        self.udp_session_active = False
        self.udp_session_start = None
        self.udp_session_last_data = None
        self.udp_session_bytes = 0
        self.udp_session_packets = 0
        self.udp_idle_samples = 0

        self.monitor_thread = hub.spawn(self._monitor)

    # -------------------------- OpenFlow helpers --------------------------

    def add_flow(self, datapath, priority, match, output_port):
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(output_port)]
        instructions = [
            parser.OFPInstructionActions(
                datapath.ofproto.OFPIT_APPLY_ACTIONS, actions
            )
        ]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
        )
        datapath.send_msg(mod)

    def install_route(self, datapath, priority, fields, output_port):
        match = datapath.ofproto_parser.OFPMatch(**fields)
        self.add_flow(datapath, priority, match, output_port)

    def add_drop_flow(self, datapath):
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            match=parser.OFPMatch(),
            instructions=[],
        )
        datapath.send_msg(mod)

    def _install_initial_flows(self, datapath):
        dpid = datapath.id
        parser = datapath.ofproto_parser

        # Explicit drop prevents uncontrolled L2 flooding in the cyclic topology.
        self.add_drop_flow(datapath)

        # IPv4 destination forwarding.
        if dpid in self.h2_path:
            self.install_route(
                datapath, 10,
                {"eth_type": 0x0800, "ipv4_dst": self.H2_IP},
                self.h2_path[dpid],
            )
        if dpid in self.h3_path:
            self.install_route(
                datapath, 10,
                {"eth_type": 0x0800, "ipv4_dst": self.H3_IP},
                self.h3_path[dpid],
            )

        # Return path to h1. s6 must distinguish h2 and h3 by ingress port.
        if dpid == 6:
            self.install_route(
                datapath, 10,
                {"in_port": 1, "eth_type": 0x0800,
                 "ipv4_src": self.H2_IP, "ipv4_dst": self.H1_IP}, 3,
            )
            self.install_route(
                datapath, 10,
                {"in_port": 2, "eth_type": 0x0800,
                 "ipv4_src": self.H3_IP, "ipv4_dst": self.H1_IP}, 4,
            )
        else:
            if dpid in self.h2_return:
                self.install_route(
                    datapath, 10,
                    {"eth_type": 0x0800, "ipv4_dst": self.H1_IP},
                    self.h2_return[dpid],
                )
            if dpid in self.h3_return:
                self.install_route(
                    datapath, 10,
                    {"eth_type": 0x0800, "ipv4_dst": self.H1_IP},
                    self.h3_return[dpid],
                )

        # UDP measurement flow. Backup-path entries are pre-installed at s3/s5;
        # failover installs higher-priority entries on the backup path.
        udp_primary_and_backup = {**self.h2_path, **self.h2_backup}
        if dpid in udp_primary_and_backup:
            self.install_route(
                datapath, 30,
                {"eth_type": 0x0800, "ip_proto": 17,
                 "ipv4_dst": self.H2_IP, "udp_dst": self.UDP_PORT},
                udp_primary_and_backup[dpid],
            )

        # ARP request path to h2, including the alternate path.
        if dpid in udp_primary_and_backup:
            self.install_route(
                datapath, 5,
                {"eth_type": 0x0806, "arp_tpa": self.H2_IP},
                udp_primary_and_backup[dpid],
            )

        # ARP request path to h3.
        if dpid in self.h3_path:
            self.install_route(
                datapath, 5,
                {"eth_type": 0x0806, "arp_tpa": self.H3_IP},
                self.h3_path[dpid],
            )

        # ARP replies from h2/h3. At s6, host-facing ingress ports differ.
        if dpid == 6:
            self.install_route(
                datapath, 5,
                {"in_port": 1, "eth_type": 0x0806,
                 "arp_spa": self.H2_IP}, 3,
            )
            self.install_route(
                datapath, 5,
                {"in_port": 2, "eth_type": 0x0806,
                 "arp_spa": self.H3_IP}, 4,
            )
        else:
            h2_arp_return = {**self.h2_return, **{5: 1, 3: 1}}
            if dpid in h2_arp_return:
                self.install_route(
                    datapath, 5,
                    {"eth_type": 0x0806, "arp_spa": self.H2_IP},
                    h2_arp_return[dpid],
                )
            if dpid in self.h3_return:
                self.install_route(
                    datapath, 5,
                    {"eth_type": 0x0806, "arp_spa": self.H3_IP},
                    self.h3_return[dpid],
                )

    # -------------------------- Switch lifecycle --------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        self._install_initial_flows(datapath)
        self.logger.info("Installed forwarding rules on s%s", datapath.id)

    # -------------------------- One-second polling -------------------------

    def _monitor(self):
        while True:
            for datapath in list(self.datapaths.values()):
                try:
                    request = datapath.ofproto_parser.OFPFlowStatsRequest(datapath)
                    datapath.send_msg(request)
                except Exception as exc:
                    self.logger.warning(
                        "Could not request flow statistics from s%s: %s",
                        datapath.id, exc,
                    )
            hub.sleep(self.POLL_INTERVAL)

    @staticmethod
    def _match_value(match, *names):
        for name in names:
            try:
                value = match[name]
                if value is not None:
                    return value
            except (KeyError, TypeError):
                continue
        return None

    def _is_target_udp_flow(self, stat):
        if stat.priority not in (30, 40):
            return False
        match = stat.match
        dst = self._match_value(match, "ipv4_dst", "nw_dst")
        proto = self._match_value(match, "ip_proto", "nw_proto")
        port = self._match_value(match, "udp_dst", "tp_dst")
        return dst == self.H2_IP and proto == 17 and port == self.UDP_PORT

    def _record_switch_udp_flows(self, dpid, stats):
        rows = []
        for stat in stats:
            if not self._is_target_udp_flow(stat):
                continue

            rows.append((stat.priority, stat.packet_count, stat.byte_count))
            key = (dpid, stat.priority, str(stat.match))
            current = (stat.byte_count, stat.packet_count)
            previous = self.switch_udp_previous.get(key)

            if previous is None:
                byte_delta = current[0] if self.udp_session_active else 0
                packet_delta = current[1] if self.udp_session_active else 0
            else:
                byte_delta = current[0] - previous[0]
                packet_delta = current[1] - previous[1]
                if byte_delta < 0:
                    byte_delta = 0
                if packet_delta < 0:
                    packet_delta = 0

            self.switch_udp_previous[key] = current

            if byte_delta or packet_delta:
                target = (
                    self.switch_udp_session
                    if self.udp_session_active
                    else self.switch_udp_pending
                )
                totals = target.setdefault(
                    (dpid, stat.priority), [0, 0]
                )
                totals[0] += packet_delta
                totals[1] += byte_delta

        self.latest_udp_flows[dpid] = rows

    def _finish_udp_session(self):
        if not self.udp_session_active:
            return
        duration = (self.udp_session_last_data or self.udp_session_start) - self.udp_session_start
        if duration <= 0:
            self.logger.warning(
                "SDN UDP transfer ended, but the sampled duration was too short "
                "to calculate a reliable average. Bytes=%d packets=%d",
                self.udp_session_bytes, self.udp_session_packets,
            )
        else:
            average_mbps = self.udp_session_bytes * 8.0 / (duration * 1_000_000.0)
            self.logger.info("========== SDN UDP TRANSFER SUMMARY ==========")
            self.logger.info("Measurement point: s1 ingress (counted once)")
            self.logger.info("Packets observed by SDN: %d", self.udp_session_packets)
            self.logger.info("Bytes observed by SDN: %d", self.udp_session_bytes)
            self.logger.info("Sampled measurement window: %.3f s", duration)
            self.logger.info("Average SDN throughput: %.3f Mbps", average_mbps)
            self.logger.info(
                "Note: 1-second polling bounds the measurement window to the "
                "polling resolution; it is not the sender's exact application duration."
            )

        self.logger.info(
            "UDP flow counters for this transfer "
            "(per-switch values are NOT additive):"
        )
        for (dpid, priority), totals in sorted(self.switch_udp_session.items()):
            packets, byte_count = totals
            route_name = (
                "backup/high-priority"
                if priority == 40 else "configured UDP flow"
            )
            self.logger.info(
                "s%s | %s | priority=%d | packets=%d | bytes=%d",
                dpid, route_name, priority, packets, byte_count,
            )
        self.logger.info("==============================================")

        self.udp_session_active = False
        self.udp_session_start = None
        self.udp_session_last_data = None
        self.udp_session_bytes = 0
        self.udp_session_packets = 0
        self.udp_idle_samples = 0

    def _update_s1_udp_measurement(self, stats, current_time):
        interval_bytes = 0
        interval_packets = 0
        seen = set()

        for stat in stats:
            if not self._is_target_udp_flow(stat):
                continue
            key = (stat.priority, str(stat.match))
            seen.add(key)
            current = (stat.byte_count, stat.packet_count)
            previous = self.previous_flow_counters.get((1, key))

            if previous is None:
                # Do not count old counters when first seeing a flow. A new
                # backup flow created during an active transfer is counted.
                if self.udp_session_active:
                    interval_bytes += current[0]
                    interval_packets += current[1]
            else:
                byte_delta = current[0] - previous[0]
                packet_delta = current[1] - previous[1]
                if byte_delta >= 0:
                    interval_bytes += byte_delta
                if packet_delta >= 0:
                    interval_packets += packet_delta

            self.previous_flow_counters[(1, key)] = current

        previous_sample = self.last_s1_sample_time
        self.last_s1_sample_time = current_time

        if interval_bytes > 0 or interval_packets > 0:
            if not self.udp_session_active:
                self.udp_session_active = True
                self.switch_udp_session = {
                    key: list(value)
                    for key, value in self.switch_udp_pending.items()
                }
                self.switch_udp_pending = {}
                self.udp_session_start = (
                    previous_sample if previous_sample is not None
                    else current_time - self.POLL_INTERVAL
                )
                self.udp_session_bytes = 0
                self.udp_session_packets = 0
                self.logger.info("SDN UDP transfer detected at s1; collecting 1-second samples.")

            sample_duration = (
                current_time - previous_sample
                if previous_sample is not None else self.POLL_INTERVAL
            )
            if sample_duration > 0 and interval_bytes > 0:
                sample_mbps = interval_bytes * 8.0 / (sample_duration * 1_000_000.0)
                self.logger.info(
                    "SDN UDP sample | interval=%.3fs | packets=%d | bytes=%d | "
                    "interval throughput=%.3f Mbps",
                    sample_duration, interval_packets, interval_bytes, sample_mbps,
                )
            self.udp_session_bytes += interval_bytes
            self.udp_session_packets += interval_packets
            self.udp_session_last_data = current_time
            self.udp_idle_samples = 0
        elif self.udp_session_active:
            self.udp_idle_samples += 1
            if self.udp_idle_samples >= self.UDP_IDLE_SECONDS:
                self._finish_udp_session()

    # -------------------------- Flow statistics ----------------------------

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        datapath = ev.msg.datapath
        dpid = datapath.id
        stats = ev.msg.body
        self._record_switch_udp_flows(dpid, stats)
        if dpid == 1:
            self._update_s1_udp_measurement(stats, time.monotonic())

    # -------------------------- Fault tolerance ----------------------------

    def _activate_failover(self):
        if self.failover_active:
            return
        self.failover_active = True
        self.logger.warning("LINK FAILURE DETECTED: s1-s2. Activating backup route.")

        # Forward direction: h1 -> s1 -> s3 -> s5 -> s6 -> h2.
        forward_rules = {
            1: [(20, {"eth_type": 0x0800, "ipv4_dst": self.H2_IP}, 3),
                (40, {"eth_type": 0x0800, "ip_proto": 17,
                      "ipv4_dst": self.H2_IP, "udp_dst": self.UDP_PORT}, 3),
                (20, {"eth_type": 0x0806, "arp_tpa": self.H2_IP}, 3)],
            3: [(20, {"eth_type": 0x0800, "ipv4_dst": self.H2_IP}, 2),
                (40, {"eth_type": 0x0800, "ip_proto": 17,
                      "ipv4_dst": self.H2_IP, "udp_dst": self.UDP_PORT}, 2)],
            5: [(20, {"eth_type": 0x0800, "ipv4_dst": self.H2_IP}, 2),
                (40, {"eth_type": 0x0800, "ip_proto": 17,
                      "ipv4_dst": self.H2_IP, "udp_dst": self.UDP_PORT}, 2)],
        }
        for dpid, rules in forward_rules.items():
            datapath = self.datapaths.get(dpid)
            if datapath:
                for priority, fields, output_port in rules:
                    self.install_route(datapath, priority, fields, output_port)

        # h2 ARP replies and IPv4 return packets go s6 -> s5 -> s3 -> s1 -> h1.
        s6 = self.datapaths.get(6)
        if s6:
            self.install_route(
                s6, 20,
                {"in_port": 1, "eth_type": 0x0800,
                 "ipv4_src": self.H2_IP, "ipv4_dst": self.H1_IP}, 4,
            )
            self.install_route(
                s6, 20,
                {"in_port": 1, "eth_type": 0x0806, "arp_spa": self.H2_IP}, 4,
            )

        return_rules = {
            5: [(20, {"eth_type": 0x0800, "ipv4_dst": self.H1_IP}, 1),
                (20, {"eth_type": 0x0806, "arp_spa": self.H2_IP}, 1)],
            3: [(20, {"eth_type": 0x0800, "ipv4_dst": self.H1_IP}, 1),
                (20, {"eth_type": 0x0806, "arp_spa": self.H2_IP}, 1)],
            1: [(20, {"eth_type": 0x0800, "ipv4_dst": self.H1_IP}, 1),
                (20, {"eth_type": 0x0806, "arp_spa": self.H2_IP}, 1)],
        }
        for dpid, rules in return_rules.items():
            datapath = self.datapaths.get(dpid)
            if datapath:
                for priority, fields, output_port in rules:
                    self.install_route(datapath, priority, fields, output_port)

        self.logger.warning(
            "BACKUP ROUTE INSTALLED: h1 -> s1 -> s3 -> s5 -> s6 -> h2"
        )

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        datapath = ev.msg.datapath
        if datapath.id != 1 or ev.msg.desc.port_no != 2:
            return
        ofproto = datapath.ofproto
        link_down = bool(ev.msg.desc.state & ofproto.OFPPS_LINK_DOWN)
        port_deleted = ev.msg.reason == ofproto.OFPPR_DELETE
        if link_down or port_deleted:
            self._activate_failover()
