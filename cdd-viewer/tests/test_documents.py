import unittest

from app.documents import (
    add_party, add_record, blank_company, collapse_current_addresses, mark_earlier_holdings, park_misfilled_addresses,
    reclaim_outdated_addresses, remember_merged_rows, restore_address_issue_fields,
    retag_undefined_address_issues, set_outdated, set_value, single_secretary_address, stamp_id_types,
)


def company():
    return {
        "id": "c1",
        "company_no": "9",
        "issued_shares": 100,
        "general": {"name_en": "Acme"},
        "directors": [{
            "party_id": "d1", "label": "DIR1", "label_raw": "DIR1", "roles": ["DIR1"],
            "row": 10, "name_en": "Existing", "outdated": False, "outdated_reasons": [],
        }],
        "shareholders": [{"party_id": "s1", "label": "SH1", "roles": ["SH1"], "row": 20, "outdated": False, "outdated_reasons": []}],
        "company_secretaries": [],
        "other_parties": [],
        "shareholding_summary": [{
            "name": "Existing", "label": "DIR1", "row": 10, "current_shares": 40,
            "counted_in_total": True,
        }],
        "issues": [],
    }


class BlankCompanyTests(unittest.TestCase):
    def test_a_new_company_starts_blank_and_can_be_named(self):
        created = blank_company()
        self.assertTrue(created["id"].startswith("new-"))
        self.assertEqual(created["company_no"], "")
        self.assertEqual(created["status"], "active")
        self.assertEqual(created["color_group"], "none")
        self.assertEqual(created["counts"]["issues"], {"error": 0, "warning": 0, "info": 0})
        named = set_value(created, ["company_no"], "9001")
        named = set_value(named, ["general", "name_en"], "North Office")
        self.assertEqual(named["company_no"], "9001")
        self.assertEqual(named["general"]["name_en"], "North Office")


class SecretaryAddressTests(unittest.TestCase):
    def test_outdated_address_stays_an_address(self):
        party = {"addresses": [
            {"lines": ["A", "B", "C", "D"], "outdated": True, "row": 8, "from": "2010-01-01"},
            {"lines": ["E", "F", "G", "H"], "outdated": False, "row": 9},
        ], "other": []}
        self.assertFalse(single_secretary_address(party))
        self.assertEqual(len(party["addresses"]), 2)
        self.assertTrue(party["addresses"][0]["outdated"])
        self.assertEqual(party["addresses"][1]["lines"], ["E", "F", "G", "H"])
        self.assertEqual(party["other"], [])

    def test_skipped_first_field_goes_to_undefined_area(self):
        party = {"addresses": [
            {"lines": ["Flat A", "1/F", "Building", "Hong Kong"], "outdated": True, "value": "kept", "row": 4},
            {"lines": ["", "Two", "Three", "Four"], "outdated": False, "value": "skipped", "row": 5},
        ], "other": []}
        self.assertTrue(single_secretary_address(party))
        self.assertEqual(len(party["addresses"]), 1)
        self.assertEqual(party["addresses"][0]["lines"][0], "Flat A")
        self.assertTrue(party["addresses"][0]["outdated"])
        self.assertEqual([item["value"] for item in party["other"]], ["skipped"])

    def test_a_single_skipped_address_goes_to_undefined(self):
        party = {"addresses": [{"lines": ["", "Two", "Three", "Four"], "value": "skipped", "row": 5}], "other": []}
        self.assertTrue(single_secretary_address(party))
        self.assertEqual(party["addresses"], [])
        self.assertEqual(party["other"][0]["value"], "skipped")

    def test_only_field_four_is_not_an_address(self):
        party = {"addresses": [
            {"lines": ["A", "B", "C", "D"], "row": 1},
            {"lines": ["", "", "", "Leftover"], "value": "Leftover", "row": 2, "outdated": True},
        ], "other": []}
        self.assertTrue(park_misfilled_addresses(party))
        self.assertEqual([item["row"] for item in party["addresses"]], [1])
        self.assertEqual(party["other"][0]["value"], "Leftover")
        self.assertTrue(party["other"][0]["outdated"])

    def test_only_the_latest_holding_stays_current(self):
        party = {
            "current_holding_row": 3,
            "shareholdings": [
                {"row": 1, "pre_transfer": False, "outdated": False, "no_of_shares": None, "transfer": {"from": "A"}},
                {"row": 3, "pre_transfer": False, "outdated": False, "no_of_shares": 10},
            ],
        }
        self.assertTrue(mark_earlier_holdings(party))
        self.assertTrue(party["shareholdings"][0]["pre_transfer"])
        self.assertFalse(party["shareholdings"][1]["pre_transfer"])
        self.assertFalse(mark_earlier_holdings(party))

    def test_front_filled_address_stays(self):
        party = {"addresses": [{"lines": ["One", "Two", "", ""]}], "other": []}
        self.assertFalse(park_misfilled_addresses(party))
        self.assertEqual(len(party["addresses"]), 1)

    def test_outdated_address_is_taken_back_from_undefined(self):
        party = {
            "addresses": [{"lines": ["E", "F", "G", "H"], "row": 9, "outdated": False}],
            "other": [{"value": "Old Road", "row": 8, "outdated": True}],
        }
        parsed = [
            {"lines": ["A", "B", "C", "D"], "value": "Old Road", "row": 8, "outdated": True, "from": "2010-01-01",
             "outdated_reasons": ["blue"]},
            {"lines": ["E", "F", "G", "H"], "value": "New Road", "row": 9, "outdated": False},
        ]
        self.assertTrue(reclaim_outdated_addresses(party, parsed))
        self.assertEqual(party["other"], [])
        restored = party["addresses"][1]
        self.assertEqual(restored["row"], 8)
        self.assertTrue(restored["outdated"])
        self.assertEqual(restored["lines"], ["A", "B", "C", "D"])
        self.assertEqual(restored["from"], "2010-01-01")
        self.assertFalse(reclaim_outdated_addresses(party, parsed))
        company = {"directors": [], "shareholders": [], "company_secretaries": [party], "other_parties": [],
                   "issues": [{"row": 8, "field": "undefined", "code": "ADDRESS_SUSPICIOUS", "severity": "warning"}]}
        parsed_company = {"issues": [{"row": 8, "field": "address.line.4", "code": "ADDRESS_SUSPICIOUS"}]}
        self.assertTrue(restore_address_issue_fields(company, parsed_company))
        self.assertEqual(company["issues"][0]["field"], "address.line.4")


class AddressCollapseTests(unittest.TestCase):
    def test_two_current_addresses_become_one_block(self):
        party = {"addresses": [
            {"lines": ["Old"], "outdated": True, "row": 1},
            {"lines": ["A", "B", "C", "D"], "outdated": False, "row": 2},
            {"lines": ["E", "F", "G", "H"], "outdated": False, "row": 3, "from": "2020-01-01"},
        ]}
        self.assertTrue(collapse_current_addresses(party))
        self.assertEqual(len(party["addresses"]), 2)
        self.assertTrue(party["addresses"][0]["outdated"])
        self.assertEqual(party["addresses"][1]["lines"], ["A, B, C, D, E", "F", "G", "H"])
        self.assertEqual(party["addresses"][1]["from"], "2020-01-01")
        self.assertEqual(party["addresses"][1]["alias_rows"], [2])
        self.assertFalse(collapse_current_addresses(party))

    def test_a_joined_address_still_points_at_the_old_row(self):
        party = {"addresses": [{"lines": ["A", "B", "C", "D"], "row": 3}], "other": []}
        parsed = [
            {"lines": ["Old"], "row": 2},
            {"lines": ["A", "B", "C", "D"], "row": 3},
        ]
        self.assertTrue(remember_merged_rows(party, parsed))
        self.assertEqual(party["addresses"][0]["alias_rows"], [2])
        self.assertFalse(remember_merged_rows(party, parsed))


class AddRecordTests(unittest.TestCase):
    def test_outdated_kinds_can_gain_a_new_record(self):
        sample = company()
        sample["branches"] = [{"label": "BR001", "record_id": "b1"}]
        sample["registered_offices"] = []
        updated = add_record(sample, {"kind": "RO", "line4": "New Office"})
        self.assertEqual(updated["registered_offices"][-1]["lines"][3], "New Office")
        updated = add_record(updated, {"kind": "BR", "label": "BR001"})
        self.assertEqual(updated["branches"][-1]["label"], "BR001")
        self.assertEqual(sum(1 for branch in updated["branches"] if branch["label"] == "BR001"), 2)
        updated = add_record(updated, {"kind": "SHARES", "no_of_shares": "100"})
        self.assertEqual(updated["share_capital"][-1]["no_of_shares"], 100)
        updated = add_record(updated, {"kind": "HOLDING", "party_id": "d1", "no_of_shares": "5", "transfer_from": "A", "transfer_to": "B"})
        holding = updated["directors"][0]["shareholdings"][-1]
        self.assertEqual(holding["no_of_shares"], 5)
        self.assertEqual(holding["transfer"]["to"], "B")
        updated = add_record(updated, {"kind": "OTHER", "label": "MB1", "name_en": "Member"})
        self.assertEqual(updated["other_parties"][-1]["label"], "MB1")


class EditFieldTests(unittest.TestCase):
    def test_form_fields_can_be_changed(self):
        sample = company()
        updated = set_value(sample, ["general", "name_en"], "Renamed")
        self.assertEqual(updated["general"]["name_en"], "Renamed")
        updated = set_value(updated, ["directors", "d1", "name_en"], "Ada")
        self.assertEqual(updated["directors"][0]["name_en"], "Ada")
        updated = set_value(updated, ["directors", "d1", "addresses", "0", "lines", "0"], "Queen Road")
        self.assertEqual(updated["directors"][0]["addresses"][0]["lines"][0], "Queen Road")
        updated = set_value(updated, ["general", "company_type"], "private")
        self.assertEqual(updated["general"]["company_type"], "private")
        updated = set_value(updated, ["general", "company_type"], "public")
        self.assertEqual(updated["general"]["company_type"], "public")
        sample["company_secretaries"] = [{"party_id": "cs1", "label": "CS1"}]
        updated = set_value(sample, ["company_secretaries", "cs1", "license_not_required"], "true")
        self.assertTrue(updated["company_secretaries"][0]["license_not_required"])
        updated = set_value(updated, ["company_secretaries", "cs1", "license_reason"], "Subsidiary")
        self.assertEqual(updated["company_secretaries"][0]["license_reason"], "Subsidiary")
        updated = set_value(updated, ["shareholders", "s1", "ubo"], "true")
        self.assertTrue(updated["shareholders"][0]["ubo"])
        updated = set_value(updated, ["shareholders", "s1", "ubo"], "false")
        self.assertFalse(updated["shareholders"][0]["ubo"])
        updated = set_value(updated, ["directors", "d1", "id_numbers", "0", "value"], "A123456(7)")
        updated = set_value(updated, ["directors", "d1", "id_numbers", "0", "id_type"], "passport")
        item = updated["directors"][0]["id_numbers"][0]
        self.assertEqual(item["value"], "A123456(7)")
        self.assertEqual(item["id_type"], "passport")
        cleared = set_value(updated, ["directors", "d1", "id_numbers", "0", "id_type"], "")
        self.assertIsNone(cleared["directors"][0]["id_numbers"][0]["id_type"])
        with self.assertRaises(ValueError):
            set_value(sample, ["directors", "d1", "id_numbers", "0", "id_type"], "other")

    def test_ro_and_outdated_records_can_be_changed(self):
        sample = company()
        sample["registered_offices"] = [{
            "record_id": "ro1",
            "address": "Old Road",
            "lines": ["", "", "", "Old Road"],
            "from": "2020-01-01",
            "to": None,
            "outdated": True,
            "outdated_reasons": ["blue"],
        }]
        updated = set_value(sample, ["registered_offices", "ro1", "lines", "3"], "New Road")
        office = updated["registered_offices"][0]
        self.assertEqual(office["lines"][3], "New Road")
        self.assertIn("New Road", office["address"])
        restored = set_outdated(updated, "ro1", False)
        self.assertFalse(restored["registered_offices"][0]["outdated"])
        self.assertNotIn("blue", restored["registered_offices"][0]["outdated_reasons"])

    def test_unknown_path_is_rejected(self):
        with self.assertRaises(ValueError):
            set_value(company(), ["issues", "0", "message"], "nope")


class AddPartyTests(unittest.TestCase):
    def test_added_person_keeps_the_same_fields_as_an_existing_card(self):
        updated = add_party(company(), {
            "kind": "SH",
            "label": "SH2",
            "name_en": "New Holder",
            "ci_no": "CI9",
            "email": "holder@example.com",
            "phone": "12345678",
            "line1": "Room 1",
            "from": "2024-01-01",
            "ubo": "true",
        })
        party = updated["shareholders"][-1]
        self.assertEqual(party["ci_no"], "CI9")
        self.assertTrue(party["ubo"])
        self.assertEqual(party["emails"][0]["value"], "holder@example.com")
        self.assertEqual(party["phones"][0]["value"], "12345678")
        self.assertEqual(party["addresses"][0]["from"], "2024-01-01")
        self.assertEqual(party["addresses"][0]["lines"][0], "Room 1")

    def test_same_number_can_be_added_again(self):
        updated = add_party(company(), {"kind": "DIR", "label": "DIR1", "name_en": "New Person"})
        labels = [party["label"] for party in updated["directors"]]
        self.assertEqual(labels, ["DIR1", "DIR1"])
        with self.assertRaises(ValueError):
            add_party(company(), {"kind": "DIR", "name_en": "Nobody"})

    def test_added_person_does_not_change_existing_fields(self):
        updated = add_party(company(), {"kind": "DIR", "label": "1", "name_en": "New Person", "line1": "Road 1"})
        self.assertEqual(updated["directors"][0]["name_en"], "Existing")
        self.assertEqual(updated["general"]["name_en"], "Acme")
        added = updated["directors"][-1]
        self.assertEqual(added["label"], "DIR1")
        self.assertEqual(added["name_en"], "New Person")
        self.assertEqual(added["addresses"][0]["lines"][0], "Road 1")
        self.assertTrue(added["added"])
        with_id = add_party(company(), {"kind": "DIR", "label": "DIR2", "id_number": "P123", "id_type": "passport"})
        stored = with_id["directors"][-1]["id_numbers"][0]
        self.assertEqual(stored["value"], "P123")
        self.assertEqual(stored["id_type"], "passport")

    def test_hkid_shape_is_remembered(self):
        party = {"id_numbers": [{"value": "A123456(7)"}, {"value": "Passport AB1", "id_type": "hkid"}]}
        self.assertTrue(stamp_id_types(party))
        self.assertEqual(party["id_numbers"][0]["id_type"], "hkid")
        self.assertEqual(party["id_numbers"][1]["id_type"], "hkid")
        self.assertFalse(stamp_id_types(party))

    def test_nested_records_can_be_marked_and_cancelled(self):
        sample = company()
        sample["directors"][0]["addresses"] = [{
            "record_id": "a1", "lines": ["A", "B", "C", "D"], "outdated": True, "outdated_reasons": ["blue"],
        }]
        sample["directors"][0]["id_numbers"] = [{
            "record_id": "i1", "value": "A123456(7)", "outdated": False, "outdated_reasons": [],
        }]
        restored = set_outdated(sample, "a1", False)
        self.assertFalse(restored["directors"][0]["addresses"][0]["outdated"])
        marked = set_outdated(restored, "i1", True)
        self.assertTrue(marked["directors"][0]["id_numbers"][0]["outdated"])
        self.assertIn("manual", marked["directors"][0]["id_numbers"][0]["outdated_reasons"])
        sample["company_secretaries"] = [{
            "party_id": "cs1",
            "addresses": [
                {"record_id": "old", "lines": ["A", "B", "C", "D"], "outdated": True, "outdated_reasons": ["blue"]},
                {"record_id": "new", "lines": ["E", "F", "G", "H"], "outdated": False, "outdated_reasons": []},
            ],
        }]
        swapped = set_outdated(sample, "old", False)
        flags = [item["outdated"] for item in swapped["company_secretaries"][0]["addresses"]]
        self.assertEqual(flags, [False, True])

    def test_manual_outdated_can_be_reversed(self):
        updated = set_outdated(company(), "d1", True)
        party = updated["directors"][0]
        self.assertTrue(party["outdated"])
        self.assertIn("manual", party["outdated_reasons"])
        self.assertFalse(updated["shareholding_summary"][0]["counted_in_total"])
        restored = set_outdated(updated, "d1", False)
        self.assertFalse(restored["directors"][0]["outdated"])
        self.assertTrue(restored["shareholding_summary"][0]["counted_in_total"])


if __name__ == "__main__":
    unittest.main()
