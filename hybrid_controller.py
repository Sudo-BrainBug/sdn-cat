from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from ryu.lib import hub

class HybridController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(HybridController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.monitor_thread = hub.spawn(self._monitor)
        
        # Pre-programmed static routes for the Spines
        self.spine_routes = {
            1: {'00:00:00:00:00:01': 1, '00:00:00:00:00:02': 1, '00:00:00:00:00:03': 1,
                '00:00:00:00:00:04': 2, '00:00:00:00:00:05': 2, '00:00:00:00:00:06': 2},
            2: {'00:00:00:00:00:01': 1, '00:00:00:00:00:02': 1, '00:00:00:00:00:03': 1,
                '00:00:00:00:00:04': 2, '00:00:00:00:00:05': 2, '00:00:00:00:00:06': 2},
            3: {'00:00:00:00:00:01': 1, '00:00:00:00:00:02': 1, '00:00:00:00:00:03': 1,
                '00:00:00:00:00:04': 2, '00:00:00:00:00:05': 2, '00:00:00:00:00:06': 2}
        }

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                ofproto = dp.ofproto
                parser = dp.ofproto_parser
                req = parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY)
                dp.send_msg(req)
                # self.logger.debug('Sent stats request to DPID %s', dp.id)
            hub.sleep(10)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER])
    def state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info('Switch registered: DPID %s', dp.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        self.logger.info("Switch connected: dpid=%s", dpid)

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.install_static_flows(datapath)

    def install_static_flows(self, datapath):
        parser = datapath.ofproto_parser
        dpid = datapath.id

        if dpid == 4:
            for mac, port in [('00:00:00:00:00:01', 4), ('00:00:00:00:00:02', 5), ('00:00:00:00:00:03', 6)]:
                self.add_flow(datapath, 10, parser.OFPMatch(eth_dst=mac), [parser.OFPActionOutput(port)])
            for mac in ['00:00:00:00:00:04', '00:00:00:00:00:05', '00:00:00:00:00:06']:
                self.add_flow(datapath, 10, parser.OFPMatch(eth_dst=mac), [parser.OFPActionOutput(1)]) # Default to Spine 1
        elif dpid == 5:
            for mac, port in [('00:00:00:00:00:04', 4), ('00:00:00:00:00:05', 5), ('00:00:00:00:00:06', 6)]:
                self.add_flow(datapath, 10, parser.OFPMatch(eth_dst=mac), [parser.OFPActionOutput(port)])
            for mac in ['00:00:00:00:00:01', '00:00:00:00:00:02', '00:00:00:00:00:03']:
                self.add_flow(datapath, 10, parser.OFPMatch(eth_dst=mac), [parser.OFPActionOutput(1)]) # Default to Spine 1
        elif dpid in self.spine_routes:
            for mac, port in self.spine_routes[dpid].items():
                self.add_flow(datapath, 10, parser.OFPMatch(eth_dst=mac), [parser.OFPActionOutput(port)])

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        pass # Silenced this so it doesn't clutter your terminal during the demo

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        
        # --- STORM CONTROL (Prevents the [] crashing error) ---
        # Ignore LLDP (0x88cc) and IPv6 (0x86dd)
        if eth.ethertype == 0x88cc or eth.ethertype == 0x86dd:
            return
            
        # Drop all MAC Broadcasts to prevent infinite loops in the Spine
        if eth.dst == 'ff:ff:ff:ff:ff:ff':
            return
        # --------------------------------------------------------
            
        in_port = msg.match['in_port']
        self.mac_to_port.setdefault(datapath.id, {})
        self.mac_to_port[datapath.id][eth.src] = in_port
        
        out_port = self.mac_to_port[datapath.id].get(eth.dst, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        
        if out_port != ofproto.OFPP_FLOOD:
            self.add_flow(datapath, 1, parser.OFPMatch(in_port=in_port, eth_dst=eth.dst), actions)
        
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
