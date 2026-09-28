package netguard

import (
	"errors"
	"net"
	"testing"
)

func TestCheck(t *testing.T) {
	tests := []struct {
		address      string
		denied       bool // without the opt-in
		allowPrivate bool // still refused with it
	}{
		{"203.0.113.7:443", false, false},
		{"[2606:4700::1111]:443", false, false},
		{"10.1.2.3:8080", true, false},
		{"100.64.0.1:80", true, false},
		{"[fd12:3456::1]:8000", true, false},
		{"127.0.0.1:8080", true, true},
		{"[::1]:8080", true, true},
		{"[::ffff:127.0.0.1]:80", true, true},
		{"169.254.169.254:80", true, true},
		{"[::ffff:169.254.169.254]:80", true, true},
		{"[fe80::1%lo0]:80", true, true},
		{"100.100.100.200:80", true, true},
		{"168.63.129.16:80", true, true},
		{"[fd00:ec2::254]:80", true, true},
		{"255.255.255.255:80", true, true},
		{"0.0.0.0:80", true, true},
		{"not-an-ip:80", true, true},
		{"no-port", true, true},
	}
	for _, tt := range tests {
		t.Run(tt.address, func(t *testing.T) {
			for _, c := range []struct {
				allowPrivate bool
				want         bool
			}{{false, tt.denied}, {true, tt.allowPrivate}} {
				err := check(tt.address, c.allowPrivate)
				var blocked *BlockedError
				if got := errors.As(err, &blocked); got != c.want {
					t.Errorf("check(%q, allowPrivate=%v) = %v, want refused=%v", tt.address, c.allowPrivate, err, c.want)
				}
			}
		})
	}
}

// The check sees the address the dialer connects to, whatever name it was
// given.
func TestDialerRefusesBeforeConnecting(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	_, port, _ := net.SplitHostPort(ln.Addr().String())

	_, err = Dialer(net.Dialer{}, true).Dial("tcp", net.JoinHostPort("localhost", port))

	var blocked *BlockedError
	if !errors.As(err, &blocked) || blocked.Class != Loopback {
		t.Fatalf("Dial = %v, want a loopback *BlockedError", err)
	}
}
