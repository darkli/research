import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mrs", ROOT / "03.generate_mrs.py")
mrs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mrs)


class SplitTests(unittest.TestCase):
    def test_interleaved_rules_and_resolve_flags_keep_their_positions(self):
        payload = ["DOMAIN,one.example", "DOMAIN-SUFFIX,example.org", "DOMAIN-KEYWORD,ads",
                   "IP-CIDR,192.0.2.0/24,no-resolve", "IP-CIDR,198.51.100.0/24",
                   "DOMAIN,two.example", "PROCESS-NAME,curl"]
        blocks = mrs.split_payload(payload)
        self.assertEqual([(b["behavior"], b["no_resolve"]) for b in blocks],
                         [("domain", False), ("classical", False), ("ipcidr", True),
                          ("ipcidr", False), ("domain", False), ("classical", False)])
        self.assertEqual([x for b in blocks for x in b["original"]], payload)
        self.assertEqual(blocks[0]["values"], ["one.example", "+.example.org"])

    def test_ipv4_mapped_ipv6_stays_classical(self):
        rule = "IP-CIDR6,::ffff:192.0.2.1/128,no-resolve"
        self.assertEqual(mrs.classify(rule), ("classical", False, rule))
        self.assertEqual(mrs.classify("IP-CIDR6,2001:db8::/32,no-resolve")[:2], ("ipcidr", True))

    def test_case_compatibility_partitions_do_not_cross_side_effects(self):
        payload = ["DOMAIN,exact.example", "DOMAIN-SUFFIX,one.org", "DOMAIN-SUFFIX,longer.example",
                   "DOMAIN-SUFFIX,two.org", "IP-CIDR,192.0.2.0/24", "DOMAIN-SUFFIX,one.org"]
        blocks = mrs.compile_blocks(mrs.split_payload(payload))
        self.assertEqual([b.get("case_guard") for b in blocks],
                         ["case-exact", "case-suffix-7", "case-suffix-14", None, "case-suffix-7"])
        self.assertEqual(sorted(x for b in blocks for x in b["original"]), sorted(payload))

    def test_weighting_never_enters_compiled_bundles(self):
        spec = importlib.util.spec_from_file_location("weighting", ROOT / "02.rule_weighting.py")
        weighting = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(weighting)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mrs" / "snapshot").mkdir(parents=True)
            (root / "no_resolve").mkdir()
            self.assertEqual(list(weighting.iter_rules_dirs(root)), [root, root / "no_resolve"])

    def test_special_and_unusual_literals_are_preserved(self):
        for rule in ["DOMAIN,*.literal.example", "DOMAIN-SUFFIX,example.org.",
                     r"DOMAIN-REGEX,^api[0-9]{1,3}\.example$", "IP-ASN,64512,no-resolve",
                     "IP-CIDR,192.0.2.0/24,src"]:
            self.assertEqual(mrs.classify(rule), ("classical", False, rule))

    def test_unknown_or_invalid_rules_fail_instead_of_disappearing(self):
        for rule in ["NEW-TYPE,x", "DOMAIN", "IP-CIDR,192.0.2.1/24", "IP-CIDR,192.0.2.0/24,unknown", None]:
            with self.subTest(rule=rule), self.assertRaises(mrs.MrsError):
                mrs.classify(rule)

    def test_canonical_domain_coverage_checks_both_directions(self):
        mrs.verify_roundtrip("domain", ["+.example.org", "sub.example.org"], "example.org\n.example.org\n")
        for exported in [".example.org\n", "+.example.org\n+.extra.org\n", "example.org\n"]:
            with self.assertRaises(mrs.MrsError):
                mrs.verify_roundtrip("domain", ["+.example.org"], exported)

    def test_ip_coverage_checks_families_and_added_addresses(self):
        mrs.verify_roundtrip("ipcidr", ["192.0.2.0/25", "192.0.2.128/25", "2001:db8::/32"],
                             "192.0.2.0/24\n2001:db8::/32\n")
        for original, exported in [(["192.0.2.0/25"], "192.0.2.0/24"),
                                   (["::ffff:192.0.2.1/128"], "192.0.2.1/32")]:
            with self.assertRaises(mrs.MrsError):
                mrs.verify_roundtrip("ipcidr", original, exported)


@unittest.skipUnless(os.environ.get("MIHOMO_BIN"), "MIHOMO_BIN is required for native converter tests")
class ConverterTests(unittest.TestCase):
    def test_native_roundtrip_and_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rules, output = root / "input", root / "output"
            rules.mkdir()
            output.mkdir()
            payload = ["DOMAIN,exact.example", "DOMAIN-SUFFIX,example.org", "DOMAIN-KEYWORD,ads",
                       "IP-CIDR,192.0.2.0/24,no-resolve", "IP-CIDR6,2001:db8::/32,no-resolve",
                       "IP-CIDR6,::ffff:192.0.2.1/128,no-resolve", "PROCESS-NAME,curl"]
            source = mrs.dump_yaml({"payload": payload})
            (rules / "sample.yaml").write_bytes(source)
            with tempfile.TemporaryDirectory(dir=output) as stage:
                manifest = mrs.make_outputs(rules, Path(stage), os.environ["MIHOMO_BIN"])
                mrs.publish(Path(stage), output)
            before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
            with tempfile.TemporaryDirectory(dir=output) as stage:
                again = mrs.make_outputs(rules, Path(stage), os.environ["MIHOMO_BIN"])
                mrs.publish(Path(stage), output)
            self.assertEqual(manifest["snapshot"], again["snapshot"])
            self.assertEqual(before, {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()})
            self.assertEqual((rules / "sample.yaml").read_bytes(), source)
            self.assertTrue(any(member.endswith(",no-resolve") for member in manifest["modes"]["normal"]["groups"]["sample"]))
            # A failed subsequent build cannot replace the last verified manifest.
            (rules / "bad.yaml").write_bytes(mrs.dump_yaml({"payload": ["UNKNOWN,example.org"]}))
            result = subprocess.run([os.sys.executable, str(ROOT / "03.generate_mrs.py"),
                                     "--rules-dir", str(rules), "--output-dir", str(output),
                                     "--mihomo", os.environ["MIHOMO_BIN"]], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads((output / "mrs_manifest.json").read_text())["snapshot"], manifest["snapshot"])


if __name__ == "__main__":
    unittest.main()
