package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/metacubex/mihomo/common/utils"
	"github.com/metacubex/mihomo/component/resource"
	C "github.com/metacubex/mihomo/constant"
	P "github.com/metacubex/mihomo/constant/provider"
	"github.com/metacubex/mihomo/rules"
	RC "github.com/metacubex/mihomo/rules/common"
	RP "github.com/metacubex/mihomo/rules/provider"
	"go.yaml.in/yaml/v3"
)

type testTunnel struct {
	providers map[string]P.RuleProvider
	callback  *utils.Callback[P.RuleProvider]
}

func (tt testTunnel) Providers() map[string]P.ProxyProvider               { return nil }
func (tt testTunnel) RuleProviders() map[string]P.RuleProvider            { return tt.providers }
func (tt testTunnel) RuleUpdateCallback() *utils.Callback[P.RuleProvider] { return tt.callback }

type manifest struct {
	Snapshot string                         `json:"snapshot"`
	Sources  map[string]struct{ Count int } `json:"sources"`
	Modes    map[string]struct {
		Providers map[string]struct {
			Behavior     string `json:"behavior"`
			Format       string `json:"format"`
			RelativePath string `json:"relative_path"`
		} `json:"providers"`
		Groups map[string][]string `json:"groups"`
	} `json:"modes"`
}

func memberExpression(member string) string {
	parts := strings.Split(member, ",")
	if len(parts) == 2 && strings.HasPrefix(parts[1], "case-") {
		guard := "(?-i)^[a-z0-9_.-]+$"
		if strings.HasPrefix(parts[1], "case-suffix-") {
			guard = "(?-i)[a-z0-9_.-]{" + strings.TrimPrefix(parts[1], "case-suffix-") + "}$"
		}
		return "(AND,((RULE-SET," + parts[0] + "),(DOMAIN-REGEX," + guard + ")))"
	}
	return "(RULE-SET," + member + ")"
}

func parse(t *testing.T, raw string) C.Rule {
	t.Helper()
	tp, payload, target, params := RC.ParseRulePayload(raw, true)
	rule, err := rules.ParseRule(tp, payload, target, params, nil)
	if err != nil {
		t.Fatalf("ParseRule %s: %v", raw, err)
	}
	return rule
}

type matchCase struct {
	host       string
	ip         netip.Addr
	resolvedIP netip.Addr
	process    string
	udp        bool
}
type outcome struct {
	Match          bool
	Target         string
	DNS            int
	ProcessLookups int
	IP             netip.Addr
	ASN            string
}

func evaluate(rule C.Rule, c matchCase) outcome {
	metadata := &C.Metadata{Host: c.host, DstIP: c.ip, SrcIP: netip.MustParseAddr("192.0.2.77"),
		SrcPort: 12345, DstPort: 443, Process: c.process, NetWork: C.TCP}
	if c.udp {
		metadata.NetWork = C.UDP
	}
	dns, process := 0, 0
	attempted, lookedUp := false, false
	helper := C.RuleMatchHelper{
		ResolveIP: func() {
			// Match the tunnel's once-per-request DNS guard, including failure.
			if !attempted && metadata.Host != "" && !metadata.DstIP.IsValid() {
				attempted = true
				dns++
				metadata.DstIP = c.resolvedIP
			}
		},
		FindProcess: func() {
			if !lookedUp {
				lookedUp = true
				process++
			}
		},
	}
	matched, target := rule.Match(metadata, helper)
	// Non-matching RuleSet and OR rules need not return the same unused adapter.
	if !matched {
		target = ""
	}
	return outcome{matched, target, dns, process, metadata.DstIP, metadata.DstIPASN}
}

func casesFor(payload []string) []matchCase {
	cases := []matchCase{{host: "not-in-rules.invalid"}, {host: "not-in-rules.invalid", resolvedIP: netip.MustParseAddr("198.51.100.19")},
		{ip: netip.MustParseAddr("192.0.2.1")}, {ip: netip.MustParseAddr("2001:db8::1")}}
	step := len(payload) / 24
	if step < 1 {
		step = 1
	}
	typeSeen := map[string]int{}
	for index, rule := range payload {
		parts := strings.Split(rule, ",")
		typ := parts[0]
		typeSeen[typ]++
		if index%step != 0 && typeSeen[typ] > 3 && index != len(payload)-1 {
			continue
		}
		value := parts[1]
		switch typ {
		case "DOMAIN", "DOMAIN-SUFFIX":
			for _, host := range []string{value, "child." + value, "UPPER." + value, "other" + value, strings.ToUpper(value), strings.ReplaceAll(value, "k", "K")} {
				cases = append(cases, matchCase{host: host}, matchCase{host: host, resolvedIP: netip.MustParseAddr("198.51.100.19")})
			}
		case "DOMAIN-KEYWORD":
			cases = append(cases, matchCase{host: "prefix-" + value + ".invalid"})
		case "IP-CIDR", "IP-CIDR6":
			p, err := netip.ParsePrefix(value)
			if err != nil {
				panic(err)
			}
			start := p.Masked().Addr()
			lastBytes := start.As16()
			bits := p.Bits()
			if start.Is4() {
				bits += 96
			}
			for bit := bits; bit < 128; bit++ {
				lastBytes[bit/8] |= 1 << uint(7-bit%8)
			}
			end := netip.AddrFrom16(lastBytes)
			if start.Is4() {
				end = end.Unmap()
			}
			for _, ip := range []netip.Addr{start, start.Next(), start.Prev(), end, end.Next()} {
				if ip.IsValid() {
					// The actual tunnel unmaps incoming IPv4-mapped IPv6 addresses.
					ip = ip.Unmap()
					cases = append(cases, matchCase{ip: ip}, matchCase{host: "dns-probe.invalid", resolvedIP: ip})
				}
			}
		case "PROCESS-NAME":
			cases = append(cases, matchCase{host: "process-probe.invalid", process: value}, matchCase{process: value})
		}
	}
	for _, host := range []string{"netflix.com", "chatgpt.com", "google.com", "youtube.com", "api.openai.com", "s3.us-east-1.amazonaws.com"} {
		cases = append(cases, matchCase{host: host})
	}
	return cases
}

func TestAllGeneratedRulesAgainstNativeClassical(t *testing.T) {
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	C.SetHomeDir(t.TempDir())
	db, err := os.ReadFile("testdata/ASN.mmdb")
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(C.Path.ASN(), db, 0600); err != nil {
		t.Fatal(err)
	}
	tt := testTunnel{map[string]P.RuleProvider{}, utils.NewCallback[P.RuleProvider]()}
	RP.SetTunnel(tt)
	raw, err := os.ReadFile(filepath.Join(root, "rules/mrs_manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var mf manifest
	if err = json.Unmarshal(raw, &mf); err != nil {
		t.Fatal(err)
	}
	for _, mode := range mf.Modes {
		for name, node := range mode.Providers {
			if _, exists := tt.providers[name]; exists {
				continue
			}
			behavior, err := P.ParseBehavior(node.Behavior)
			if err != nil {
				t.Fatal(err)
			}
			format, err := P.ParseRuleFormat(node.Format)
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(root, "rules/rules_set", node.RelativePath)
			provider := RP.NewRuleSetProvider(name, behavior, format, 0, resource.NewFileVehicle(path), nil, nil, rules.ParseRule)
			if err = provider.Initial(); err != nil {
				t.Fatal(err)
			}
			tt.providers[name] = provider
		}
	}
	sources := make([]string, 0, len(mf.Sources))
	for name := range mf.Sources {
		sources = append(sources, name)
	}
	sort.Strings(sources)
	comparisons := 0
	for _, source := range sources {
		raw, err := os.ReadFile(filepath.Join(root, "rules/rules_set", source))
		if err != nil {
			t.Fatal(err)
		}
		var document struct {
			Payload []string `yaml:"payload"`
		}
		if err = yaml.Unmarshal(raw, &document); err != nil {
			t.Fatal(err)
		}
		baseline := RP.NewInlineProvider("baseline", P.Classical, document.Payload, rules.ParseRule)
		if baseline.Count() != len(document.Payload) {
			t.Fatalf("native classical skipped a rule in %s: %d/%d", source, baseline.Count(), len(document.Payload))
		}
		tt.providers["baseline"] = baseline
		oldRule := parse(t, "RULE-SET,baseline,unchanged-policy")
		mode := "normal"
		if strings.HasPrefix(source, "no_resolve/") {
			mode = "no_resolve"
		}
		name := strings.TrimSuffix(filepath.Base(source), ".yaml")
		members := mf.Modes[mode].Groups[name]
		if len(members) == 0 {
			t.Fatalf("missing group %s", source)
		}
		children := make([]string, len(members))
		for i, member := range members {
			children[i] = memberExpression(member)
		}
		newRule := parse(t, "OR,("+strings.Join(children, ",")+"),unchanged-policy")
		for i, c := range casesFor(document.Payload) {
			for _, udp := range []bool{false, true} {
				c.udp = udp
				old, new := evaluate(oldRule, c), evaluate(newRule, c)
				comparisons++
				if old != new {
					t.Fatalf("%s case %d udp=%v host=%s ip=%s DNS-result=%s: old=%+v new=%+v", source, i, udp, c.host, c.ip, c.resolvedIP, old, new)
				}
			}
		}
		delete(tt.providers, "baseline")
	}
	report := map[string]any{"mihomo": "v1.19.33", "snapshot": mf.Snapshot, "manifest_sha256": fmt.Sprintf("%x", sha256.Sum256(raw)), "classical_files": len(sources), "comparisons": comparisons,
		"result": "passed", "checks": []string{"match", "policy", "DNS queries", "process lookup", "resulting IP", "ASN metadata", "TCP/UDP", "CIDR boundaries", "IPv4-mapped IPv6"}}
	text, _ := json.MarshalIndent(report, "", "  ")
	if path := os.Getenv("MRS_NATIVE_REPORT"); path != "" {
		if err = os.WriteFile(path, append(text, '\n'), 0644); err != nil {
			t.Fatal(err)
		}
	}
	fmt.Printf("Native equivalence passed: %d files, %d comparisons\n", len(sources), comparisons)
}

// This additionally consumes rules actually emitted by the Bash subscription
// generator, rather than reconstructing expressions from the manifest.
func TestRenderedSubscriptionAgainstNativeClassical(t *testing.T) {
	configPath := os.Getenv("MRS_RENDERED_CONFIG")
	bindingsPath := os.Getenv("MRS_RENDERED_BINDINGS")
	if configPath == "" || bindingsPath == "" {
		t.Skip("set MRS_RENDERED_CONFIG and MRS_RENDERED_BINDINGS for cross-repository integration")
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	C.SetHomeDir(t.TempDir())
	db, err := os.ReadFile("testdata/ASN.mmdb")
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(C.Path.ASN(), db, 0600); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(root, "rules/mrs_manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var mf manifest
	if err = json.Unmarshal(raw, &mf); err != nil {
		t.Fatal(err)
	}
	tt := testTunnel{map[string]P.RuleProvider{}, utils.NewCallback[P.RuleProvider]()}
	RP.SetTunnel(tt)
	for name, node := range mf.Modes["no_resolve"].Providers {
		behavior, err := P.ParseBehavior(node.Behavior)
		if err != nil {
			t.Fatal(err)
		}
		format, err := P.ParseRuleFormat(node.Format)
		if err != nil {
			t.Fatal(err)
		}
		provider := RP.NewRuleSetProvider(name, behavior, format, 0,
			resource.NewFileVehicle(filepath.Join(root, "rules/rules_set", node.RelativePath)), nil, nil, rules.ParseRule)
		if err = provider.Initial(); err != nil {
			t.Fatal(err)
		}
		tt.providers[name] = provider
	}
	configRaw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var config struct {
		Rules []string `yaml:"rules"`
	}
	if err = yaml.Unmarshal(configRaw, &config); err != nil {
		t.Fatal(err)
	}
	bindingsRaw, err := os.ReadFile(bindingsPath)
	if err != nil {
		t.Fatal(err)
	}
	bindings := strings.Split(strings.TrimSpace(string(bindingsRaw)), "\n")
	if len(config.Rules) != len(bindings)+1 || config.Rules[len(bindings)] != "MATCH,test-policy" {
		t.Fatal("binding count/order or final rule changed")
	}
	comparisons := 0
	for i, binding := range bindings {
		pair := strings.SplitN(binding, "|", 2)
		if len(pair) != 2 {
			t.Fatal("invalid binding fixture")
		}
		source := "no_resolve/" + pair[0] + ".yaml"
		if _, exists := mf.Sources[source]; !exists {
			source = pair[0] + ".yaml"
		}
		sourceRaw, err := os.ReadFile(filepath.Join(root, "rules/rules_set", source))
		if err != nil {
			t.Fatal(err)
		}
		var document struct {
			Payload []string `yaml:"payload"`
		}
		if err = yaml.Unmarshal(sourceRaw, &document); err != nil {
			t.Fatal(err)
		}
		tt.providers["baseline"] = RP.NewInlineProvider("baseline", P.Classical, document.Payload, rules.ParseRule)
		oldRule := parse(t, "RULE-SET,baseline,"+pair[1])
		newRule := parse(t, config.Rules[i])
		if newRule.Adapter() != pair[1] {
			t.Fatalf("policy changed for binding %s", pair[0])
		}
		for _, c := range casesFor(document.Payload) {
			for _, udp := range []bool{false, true} {
				c.udp = udp
				old, new := evaluate(oldRule, c), evaluate(newRule, c)
				comparisons++
				if old != new {
					t.Fatalf("rendered binding %s differs: old=%+v new=%+v", pair[0], old, new)
				}
			}
		}
	}
	report := map[string]any{"snapshot": mf.Snapshot, "logical_bindings": len(bindings), "comparisons": comparisons,
		"result": "passed", "rendered_config_sha256": fmt.Sprintf("%x", sha256.Sum256(configRaw))}
	if path := os.Getenv("MRS_SUBSCRIPTION_REPORT"); path != "" {
		text, _ := json.MarshalIndent(report, "", "  ")
		if err = os.WriteFile(path, append(text, '\n'), 0644); err != nil {
			t.Fatal(err)
		}
	}
	fmt.Printf("Rendered subscription equivalence passed: %d bindings, %d comparisons\n", len(bindings), comparisons)
}
