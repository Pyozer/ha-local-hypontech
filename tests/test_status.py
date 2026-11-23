"""Availability model: what an inverter's silence means."""
import unittest

import support  # noqa: F401  (puts the add-on sources on sys.path)

import hypon_status as hs

THRESHOLD = 300
ABSENT = hs.Link(mac="aa:bb:cc:dd:ee:ff", checked=True)   # the network was searched: nothing there


def decide(silent_for=THRESHOLD + 1, link=ABSENT, threshold=THRESHOLD):
    return hs.decide(silent_for, threshold, link)


class DecideTests(unittest.TestCase):
    def test_frames_arriving_means_online_whatever_the_network_says(self):
        for link in (hs.NO_LINK, ABSENT, hs.Link(alive=True), hs.Link(moved_to="192.168.1.140")):
            with self.subTest(link=link):
                self.assertEqual(decide(silent_for=0, link=link), hs.ONLINE)
                self.assertEqual(decide(silent_for=THRESHOLD, link=link), hs.ONLINE)

    def test_silent_and_nowhere_on_the_network_means_off(self):
        # No light on the panels: night, storm, heavy shade. Its production really is 0.
        self.assertEqual(decide(link=ABSENT), hs.SLEEPING)

    def test_silent_but_answering_the_network_is_a_problem(self):
        self.assertEqual(decide(link=hs.Link(alive=True, checked=True)), hs.UNREACHABLE)

    def test_an_inverter_seen_under_another_ip_has_moved(self):
        moved = hs.Link(moved_to="192.168.1.140", checked=True)
        self.assertEqual(decide(link=moved), hs.IP_CHANGED)

    def test_a_move_has_priority_over_a_sign_of_life(self):
        both = hs.Link(alive=True, moved_to="192.168.1.140", checked=True)
        self.assertEqual(decide(link=both), hs.IP_CHANGED)

    def test_positive_evidence_does_not_need_the_network_to_have_been_searched(self):
        self.assertEqual(decide(link=hs.Link(moved_to="192.168.1.140")), hs.IP_CHANGED)
        self.assertEqual(decide(link=hs.Link(alive=True)), hs.UNREACHABLE)

    def test_without_a_look_at_the_network_it_is_not_taken_for_off(self):
        # We cannot see: claiming "off" (a 0 W that may be false) would be dishonest.
        self.assertEqual(decide(link=hs.NO_LINK), hs.UNREACHABLE)

    def test_an_inverter_that_had_faded_is_off_even_if_its_collector_answers(self):
        self.assertEqual(hs.decide(THRESHOLD + 1, THRESHOLD, hs.Link(alive=True, checked=True), faded=True),
                         hs.SLEEPING)

    def test_an_inverter_that_had_faded_is_off_even_if_the_network_was_not_looked_at(self):
        self.assertEqual(hs.decide(THRESHOLD + 1, THRESHOLD, hs.NO_LINK, faded=True), hs.SLEEPING)

    def test_a_move_has_priority_over_fading(self):
        moved = hs.Link(moved_to="192.168.1.140", checked=True)
        self.assertEqual(hs.decide(THRESHOLD + 1, THRESHOLD, moved, faded=True), hs.IP_CHANGED)

    def test_fading_means_nothing_while_frames_arrive(self):
        self.assertEqual(hs.decide(0, THRESHOLD, hs.NO_LINK, faded=True), hs.ONLINE)

    def test_without_fading_the_rules_are_unchanged(self):
        alive = hs.Link(alive=True)
        self.assertEqual(hs.decide(THRESHOLD + 1, THRESHOLD, alive, faded=False), hs.UNREACHABLE)

    def test_a_status_is_always_one_of_the_declared_ones(self):
        links = (hs.NO_LINK, ABSENT, hs.Link(alive=True), hs.Link(moved_to="1.2.3.4", checked=True))
        for link in links:
            for silent_for in (0, THRESHOLD + 1):
                self.assertIn(decide(silent_for=silent_for, link=link), hs.STATUSES)

    def test_only_unreachable_and_ip_changed_make_the_measurements_unavailable(self):
        self.assertEqual(set(hs.UNAVAILABLE_STATUSES), {hs.UNREACHABLE, hs.IP_CHANGED})
        self.assertEqual(set(hs.STATUSES) - set(hs.UNAVAILABLE_STATUSES), {hs.ONLINE, hs.SLEEPING})


class CadenceTests(unittest.TestCase):
    def test_nothing_is_known_at_first(self):
        self.assertIsNone(hs.Cadence().median())

    def test_median_of_the_recent_gaps(self):
        cadence = hs.Cadence()
        for gap in (10, 30, 20):
            cadence.add(gap)
        self.assertEqual(cadence.median(), 20)

    def test_a_night_or_an_outage_is_not_a_reporting_rhythm(self):
        cadence = hs.Cadence()
        cadence.add(60)
        cadence.add(hs.CADENCE_MAX_GAP_S + 1)    # overnight silence
        cadence.add(0)
        cadence.add(-5)
        self.assertEqual(cadence.median(), 60)

    def test_only_the_most_recent_gaps_are_kept(self):
        cadence = hs.Cadence()
        for _ in range(hs.CADENCE_SAMPLES):
            cadence.add(10)
        for _ in range(hs.CADENCE_SAMPLES):
            cadence.add(100)
        self.assertEqual(cadence.median(), 100)


class StaleThresholdTests(unittest.TestCase):
    def test_the_configured_grace_applies_while_the_rhythm_is_unknown(self):
        self.assertEqual(hs.stale_threshold(300, hs.Cadence()), 300)

    def test_a_fast_reporter_never_shortens_the_grace(self):
        cadence = hs.Cadence()
        cadence.add(10)
        self.assertEqual(hs.stale_threshold(300, cadence), 300)

    def test_a_slow_reporter_is_given_a_few_periods(self):
        cadence = hs.Cadence()
        cadence.add(400)
        self.assertEqual(hs.stale_threshold(300, cadence), hs.STALE_FACTOR * 400)

    def test_a_zero_grace_stays_zero_until_the_rhythm_is_known(self):
        self.assertEqual(hs.stale_threshold(0, hs.Cadence()), 0)


class LinkTests(unittest.TestCase):
    def test_default_link_knows_nothing(self):
        link = hs.Link()
        self.assertEqual((link.mac, link.alive, link.moved_to, link.checked),
                         (None, False, None, False))
        self.assertEqual(hs.NO_LINK, link)

    def test_links_are_immutable(self):
        with self.assertRaises(AttributeError):
            hs.NO_LINK.alive = True


if __name__ == "__main__":
    unittest.main()
