from mininet.topo import Topo


class FaultTolerantTopo(Topo):

    def build(self):

        # Hosts
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        h3 = self.addHost('h3')

        # Switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')
        s5 = self.addSwitch('s5')
        s6 = self.addSwitch('s6')

        # Hosts
        self.addLink(h1, s1)
        self.addLink(s6, h2)
        self.addLink(s6, h3)

        # Left path
        self.addLink(s1, s2)
        self.addLink(s2, s4)
        self.addLink(s4, s6)

        # Right path
        self.addLink(s1, s3)
        self.addLink(s3, s5)
        self.addLink(s5, s6)


topos = {
    'faulttopo': lambda: FaultTolerantTopo()
}
