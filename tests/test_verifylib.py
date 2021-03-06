#!/usr/bin/env python

"""
<Program Name>
  test_verifylib.py

<Author>
  Lukas Puehringer <lukas.puehringer@nyu.edu>

<Started>
  Nov 07, 2016

<Copyright>
  See LICENSE for licensing information.

<Purpose>
  Test verifylib functions.

"""

import os
import shutil
import copy
import tempfile
import unittest
import glob
from mock import patch
from datetime import datetime
from dateutil.relativedelta import relativedelta

import in_toto.settings
from in_toto.models.metadata import Metablock
from in_toto.models.link import Link, FILENAME_FORMAT
from in_toto.models.layout import (Step, Inspection, Layout,
    SUBLAYOUT_LINK_DIR_FORMAT)
from in_toto.verifylib import (verify_delete_rule, verify_create_rule,
    verify_modify_rule, verify_allow_rule, verify_disallow_rule,
    verify_match_rule, verify_item_rules, verify_all_item_rules,
    verify_command_alignment, run_all_inspections, in_toto_verify,
    verify_sublayouts, get_summary_link, _raise_on_bad_retval,
    load_links_for_layout, verify_link_signature_thresholds,
    verify_threshold_constraints)
from in_toto.exceptions import (RuleVerificationError,
    SignatureVerificationError, LayoutExpiredError, BadReturnValueError,
    ThresholdVerificationError)
from in_toto.util import import_rsa_key_from_file, import_rsa_public_keys_from_files_as_dict
import in_toto.gpg.functions

import securesystemslib.exceptions
import in_toto.exceptions


class Test_RaiseOnBadRetval(unittest.TestCase):
  """Tests internal function that raises an exception if the passed
  "return_value" is not and integer and not zero. """

  def test_zero_return_value(self):
    """Don't raise exception on zero return value. """
    _raise_on_bad_retval(0)
    _raise_on_bad_retval(0, "command")

  def test_non_int_return_value(self):
    """Raise exception on non-int return value. """
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval("bad retval")
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval("bad retval", "bad command")

  def test_non_zero_return_value(self):
    """Raise exception on non-zero return value. """
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval(1)
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval(-1, "bad command")


class TestRunAllInspections(unittest.TestCase):
  """Test verifylib.run_all_inspections(layout)"""

  @classmethod
  def setUpClass(self):
    """
    Create layout with dummy inpsection.
    Create and change into temp test directory with dummy artifact."""

    # Create layout with one inspection
    self.layout = Layout.read({
        "_type": "layout",
        "steps": [],
        "inspect": [{
          "name": "touch-bar",
          "run": ["touch", "bar"],
        }]
      })

    # Create directory where the verification will take place
    self.working_dir = os.getcwd()
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)
    open("foo", "w").write("foo")

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp test directory. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_inpsection_artifacts_with_base_path_ignored(self):
    """Create new dummy test dir and set as base path, must ignore. """
    ignore_dir = os.path.realpath(tempfile.mkdtemp())
    ignore_foo = os.path.join(ignore_dir, "ignore_foo")
    open(ignore_foo, "w").write("ignore foo")
    in_toto.settings.ARTIFACT_BASE_PATH = ignore_dir

    run_all_inspections(self.layout)
    link = Metablock.load("touch-bar.link")
    self.assertListEqual(list(link.signed.materials.keys()), ["foo"])
    self.assertListEqual(sorted(list(link.signed.products.keys())), sorted(["foo", "bar"]))

    in_toto.settings.ARTIFACT_BASE_PATH = None
    shutil.rmtree(ignore_dir)

  def test_inspection_fail_with_non_zero_retval(self):
    """Test fail run inspections with non-zero return value. """
    layout = Layout.read({
        "_type": "layout",
        "steps": [],
        "inspect": [{
          "name": "non-zero-inspection",
          "run": ["expr", "1", "/", "0"],
        }]
    })
    with self.assertRaises(BadReturnValueError):
      run_all_inspections(layout)


class TestVerifyCommandAlignment(unittest.TestCase):
  """Test verifylib.verify_command_alignment(command, expected_command)"""

  def setUp(self):
    self.command = ["vi", "file1", "file2"]

  def test_commands_align(self):
    """Cmd and expected cmd are equal, passes. """
    expected_command = ["vi", "file1", "file2"]
    verify_command_alignment(self.command, expected_command)

  def test_commands_do_not_fully_align_log_warning(self):
    """Cmd and expected cmd differ slightly. """
    expected_command = ["/usr/bin/vi", "file1", "file2"]

    with patch("in_toto.verifylib.log") as mock_logging:
      verify_command_alignment(self.command, expected_command)
      mock_logging.warning.assert_called_with("Run command '{0}'"
          " differs from expected command '{1}'"
          .format(self.command, expected_command))

  def test_commands_do_not_align_at_all_log_warning(self):
    """Cmd and expected cmd differ completely. """
    expected_command = ["make install"]

    with patch("in_toto.verifylib.log") as mock_logging:
      verify_command_alignment(self.command, expected_command)
      mock_logging.warning.assert_called_with("Run command '{0}'"
          " differs from expected command '{1}'"
          .format(self.command, expected_command))


class TestVerifyDeleteRule(unittest.TestCase):
  """Test verify_delete_rule
  takes a rule ["DELETE", "<path pattern>"], a product queue and a
  material queue.
  Materials filtered by path pattern can't appear as products.
  Returns the material queue minus the deleted materials.
  """

  def test_fail_delete_file(self):
    """["DELETE", "foo"], foo still in products (not deleted), fails. """
    materials_queue = ["foo"]
    products_queue = ["foo"]
    rule = ["DELETE", "foo"]
    with self.assertRaises(RuleVerificationError):
      verify_delete_rule(rule, materials_queue, products_queue)

  def test_fail_delete_star(self):
    """["DELETE", "*"], not all (*) materials were deleted, fails. """

    materials_queue = ["foo", "bar"]
    products_queue = ["foo"]
    rule = ["DELETE", "*"]

    with self.assertRaises(RuleVerificationError):
        verify_delete_rule(rule, materials_queue, products_queue)

  def test_pass_delete_file(self):
    """["DELETE", "foo"], foo not in products (deleted), passes. """
    materials_queue = ["foo", "baz"]
    products_queue = []
    rule = ["DELETE", "foo"]
    queue = verify_delete_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, ["baz"])

  def test_pass_delete_star(self):
    """["DELETE", "*"], no materials appear in products (deleted), passes. """
    materials_queue = ["foo", "baz"]
    products_queue = []
    rule = ["DELETE", "*"]
    queue = verify_delete_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, [])

  def test_pass_delete_nothing_nothing_filtered(self):
    """["DELETE", "bar"], bar in products but not in materials, passes. """
    materials_queue = []
    products_queue = ["bar"]
    rule = ["DELETE", "bar"]
    queue = verify_delete_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, [])

  def test_pass_delete_nothing_empty_queue(self):
    """["DELETE", "*"], nothing in materials, passes. """
    materials_queue = []
    products_queue = ["foo", "bar", "baz"]
    rule = ["DELETE", "*"]
    queue = verify_delete_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, [])


class TestVerifyCreateRule(unittest.TestCase):
  """Test verifylib.verify_create_rule
  takes a rule ["CREATE", "<path pattern>"], a product queue and a
  material queue.
  Products filtered by path pattern can't appear as materials.
  Returns the product queue minus the created products.
  """

  def test_fail(self):
    """Different scenarios for failing create rule verification"""
    # Foo already in materials (not created)
    materials_queue = ["foo"]
    products_queue = ["foo"]
    rule = ["CREATE", "foo"]
    result = verify_create_rule(rule, materials_queue, products_queue)
    self.assertEqual(['foo'], result)

    # Not all (*) products newly created
    materials_queue = ["foo"]
    products_queue = ["foo", "bar"]
    rule = ["CREATE", "*"]
    result = verify_create_rule(rule, materials_queue, products_queue)
    self.assertEqual(['foo'], result)

  def test_pass(self):
    """"Different scenarios for passing create rule verification. """
    # Foo created
    materials_queue = ["bar"]
    products_queue = ["foo", "bar"]
    rule = ["CREATE", "foo"]
    queue = verify_create_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, ["bar"])

    # * created
    materials_queue = []
    products_queue = ["foo", "bar", "baz"]
    rule = ["CREATE", "*"]
    queue = verify_create_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, [])

    # No products filtered by pattern
    materials_queue = ["foo", "bar"]
    products_queue = []
    rule = ["CREATE", "*"]
    queue = verify_create_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, [])

    # No products filtered by pattern (pass seems strange)
    materials_queue = ["foo", "bar"]
    products_queue = []
    rule = ["CREATE", "baz"]
    queue = verify_create_rule(rule, materials_queue, products_queue)
    self.assertListEqual(queue, [])


class TestVerifyModifyRule(unittest.TestCase):
  """Test verifylib.verify_modify_rule
  takes a rule ["MODIFY", "<path pattern>"], a product queue, a
  material queue, a material dict and a product dict.

  The sets of materials and products from the queue filtered by the path pattern
  must be equal in terms of paths.
  And each material-product must have different hashes.

  Returns updated materials and products queues minus the respective filtered
  artifacts.
  """

  @classmethod
  def setUpClass(self):
    sha256_1 = ("d65165279105ca6773180500688df4bd"
                  "c69a2c7b771752f0a46ef120b7fd8ec3")

    sha256_2 = ("155c693a6b7481f48626ebfc545f0523"
                  "6df679f0099225d6d0bc472e6dd21155")

    self.materials = {
      "foo": {"sha256": sha256_1},
      "bar": {"sha256": sha256_1}
    }
    self.products = {
      "foo": {"sha256": sha256_2},
      "bar": {"sha256": sha256_1}
    }

  def test_pass(self):
    """Different scenarios for passing modify rule verification. """

    # Modify single file
    materials_queue = ["foo"]
    products_queue = ["foo", "bar"]
    rule = ["MODIFY", "foo"]
    m_queue, p_queue = verify_modify_rule(rule, materials_queue, products_queue,
        self.materials, self.products)
    self.assertListEqual(m_queue, ["foo"])
    self.assertListEqual(p_queue, ["bar"])

    # Modify all files from queue
    materials_queue = ["foo"]
    products_queue = ["foo"]
    rule = ["MODIFY", "*"]
    m_queue, p_queue = verify_modify_rule(rule, materials_queue, products_queue,
        self.materials, self.products)
    self.assertListEqual(m_queue, ["foo"])
    self.assertListEqual(p_queue, [])

    # Nothing filtered by pattern, still passes (seems strange)
    rule = ["MODIFY", "baz"]
    m_queue, p_queue = verify_modify_rule(rule, materials_queue, products_queue,
        self.materials, self.products)
    self.assertListEqual(m_queue, p_queue, [])

    # Nothing filtered by pattern, still passes
    rule = ["MODIFY", "*"]
    materials_queue = []
    products_queue = []
    m_queue, p_queue = verify_modify_rule(rule, materials_queue, products_queue,
        self.materials, self.products)
    self.assertListEqual(m_queue, p_queue, [])

  def test_fail(self):
    """Different scenarios for failing create rule verification. """
    materials_queue = ["bar", "foo"]
    products_queue = ["bar", "foo"]

    # Single file not modified
    rule = ["MODIFY", "bar"]
    result = verify_modify_rule(rule, materials_queue, products_queue,
        self.materials, self.products)
    self.assertEquals((materials_queue, products_queue),
        (sorted(result[0]), sorted(result[1])))

    # Some files not modified
    rule = ["MODIFY", "*"]
    result = verify_modify_rule(rule, materials_queue, products_queue,
          self.materials, self.products)
    self.assertEquals((materials_queue, ['bar']),
        (sorted(result[0]), sorted(result[1])))

    # Pattern filters bar as material but not as product
    materials_queue = ["bar", "foo"]
    products_queue = ["foo"]
    result = verify_modify_rule(rule, materials_queue, products_queue,
          self.materials, self.products)
    self.assertEquals((materials_queue, []),
        (sorted(result[0]), sorted(result[1])))

    # Pattern filters bar as product but not as material
    materials_queue = ["foo"]
    products_queue = ["bar", "foo"]
    result = verify_modify_rule(rule, materials_queue, products_queue,
          self.materials, self.products)
    self.assertEquals((materials_queue, ['bar']),
        (sorted(result[0]), sorted(result[1])))


class TestVerifyAllowRule(unittest.TestCase):
  """ Verify verifylib.verify_allow_rule
  takes a rule ["ALLOW", "<path pattern>"] and an artifact queue
  (materials or products).

  The rule never fails but only pops filtered items from the artifacts queue
  and returns the updated queue.
  """

  def test(self):
    """Test returned artifact queue. """
    queue = ["foo", "bar", "foobar"]
    rule = ["ALLOW", "foo"]
    queue = verify_allow_rule(rule, queue)
    self.assertListEqual(sorted(queue), ["bar", "foobar"])

    queue = ["foo", "bar", "foobar"]
    rule = ["ALLOW", "foo*"]
    queue = verify_allow_rule(rule, queue)
    self.assertListEqual(queue, ["bar"])

    rule = ["ALLOW", "*"]
    queue = verify_allow_rule(rule, queue)
    self.assertListEqual(queue, [])


class TestVerifyDisallowRule(unittest.TestCase):
  """ Verify verifylib.verify_disallow_rule
  takes a rule ["DISALLOW", "<path pattern>"] and an artifact queue
  (materials or products).
  Fails if an artifact is filtered by the pattern.
  """

  def test_pass(self):
    """ Test different passing disallow rule scenarios. """
    queue = ["foo", "bar", "foobar"]
    rule = ["DISALLOW", "baz"]
    verify_disallow_rule(rule, queue)

    queue = []
    rule = ["DISALLOW", "*"]
    verify_disallow_rule(rule, queue)


  def test_fail(self):
    """ Test different failing disallow rule scenarios. """
    queue = ["foo", "bar", "foobar"]
    rule = ["DISALLOW", "foo"]
    with self.assertRaises(RuleVerificationError):
      verify_disallow_rule(rule, queue)

    queue = ["foo", "bar", "foobar"]
    rule = ["DISALLOW", "foo*"]
    with self.assertRaises(RuleVerificationError):
      verify_disallow_rule(rule, queue)

    queue = ["foo", "bar", "foobar"]
    rule = ["DISALLOW", "*"]
    with self.assertRaises(RuleVerificationError):
      verify_disallow_rule(rule, queue)


class TestVerifyMatchRule(unittest.TestCase):
  """Test verifylib.verify_match_rule(rule, artifact_queue, artifacts, links) """

  def setUp(self):
    """Setup artifact queues, artifacts dictionary and Link dictionary. """

    # Dummy artifact hashes
    self.sha256_foo = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"
    self.sha256_foobar = \
        "155c693a6b7481f48626ebfc545f05236df679f0099225d6d0bc472e6dd21155"
    self.sha256_bar = \
        "cfdaaf1ab2e4661952a9dec5e8fa3c360c1b06b1a073e8493a7c46d2af8c504b"
    self.sha256_barfoo = \
        "2036784917e49b7685c7c17e03ddcae4a063979aa296ee5090b5bb8f8aeafc5d"

    # Link dictionary containing dummy artifacts related to Steps the rule is
    # matched with (match destination).
    materials = {
      "foo": {"sha256": self.sha256_foo},
      "foobar": {"sha256": self.sha256_foobar},
      "dev/foo": {"sha256": self.sha256_foo},
      "dev/foobar": {"sha256": self.sha256_foobar}

    }
    products = {
      "bar": {"sha256": self.sha256_bar},
      "barfoo": {"sha256": self.sha256_barfoo},
      "dev/bar": {"sha256": self.sha256_bar},
      "dev/barfoo": {"sha256": self.sha256_barfoo},
      }

    # Note: For simplicity the Links don't have all usually required fields set
    self.links = {
        "link-1" : Metablock(signed=Link(
            name="link-1", materials=materials, products=products)),
    }


  def test_pass_match_material(self):
    """["MATCH", "foo", "WITH", "MATERIALS", "FROM", "link-1"],
    source artifact foo and destination material foo hashes match, passes. """

    rule = ["MATCH", "foo", "WITH", "MATERIALS", "FROM", "link-1"]
    artifacts = {
      "foo": {"sha256": self.sha256_foo},
      "bar": {"sha256": self.sha256_bar}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["bar"])


  def test_pass_match_product(self):
    """["MATCH", "bar", "WITH", "PRODUCTS", "FROM", "link-1"],
    source artifact bar and destination product bar hashes match, passes. """

    rule = ["MATCH", "bar", "WITH", "PRODUCTS", "FROM", "link-1"]
    artifacts = {
      "foo": {"sha256": self.sha256_foo},
      "bar": {"sha256": self.sha256_bar}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["foo"])


  def test_pass_match_in_source_dir_with_materials(self):
    """["MATCH", "foo", "IN", "dist", "WITH", "MATERIALS", "FROM", "link-1"],
    source artifact dist/foo and destination material foo hashes match, passes. """

    for slash in ["", "/"]:
      rule = ["MATCH", "foo", "IN", "dist" + slash, "WITH", "MATERIALS", "FROM",
        "link-1"]
      artifacts = {
        "dist/foo": {"sha256": self.sha256_foo},
        "dist/bar": {"sha256": self.sha256_bar}
      }
      queue = list(artifacts.keys())
      self.assertListEqual(
          verify_match_rule(rule, queue, artifacts, self.links), ["dist/bar"])

  def test_pass_match_in_source_dir_with_products(self):
    """["MATCH", "bar", "IN", "dist", "WITH", "PRODUCTS", "FROM", "link-1"],
    source artifact dist/bar and destination product bar hashes match, passes. """

    for slash in ["", "/"]:
      rule = ["MATCH", "bar", "IN", "dist" + slash, "WITH", "PRODUCTS", "FROM",
          "link-1"]
      artifacts = {
        "dist/bar": {"sha256": self.sha256_bar},
        "dist/foo": {"sha256": self.sha256_foo}
      }
      queue = list(artifacts.keys())
      self.assertListEqual(
          verify_match_rule(rule, queue, artifacts, self.links), ["dist/foo"])

  def test_pass_match_with_materials_in_destination_dir(self):
    """["MATCH", "foo", "WITH", "MATERIALS", "IN", "dev", "FROM", "link-1"],
    source artifact foo and destination material dev/foo hashes match, passes. """

    for slash in ["", "/"]:
      rule = ["MATCH", "foo", "WITH", "MATERIALS", "IN", "dev" + slash, "FROM",
          "link-1"]
      artifacts = {
        "foo": {"sha256": self.sha256_foo},
        "bar": {"sha256": self.sha256_bar}
      }
      queue = list(artifacts.keys())
      self.assertListEqual(
          verify_match_rule(rule, queue, artifacts, self.links), ["bar"])

  def test_pass_match_with_products_in_destination_dir(self):
    """["MATCH", "bar", "WITH", "PRODUCTS", "IN", "dev", "FROM", "link-1"],
    source artifact bar and destination product dev/bar hashes match, passes. """

    for slash in ["", "/"]:
      rule = ["MATCH", "bar", "WITH", "PRODUCTS", "IN", "dev" + slash, "FROM",
          "link-1"]
      artifacts = {
        "bar": {"sha256": self.sha256_bar},
        "foo": {"sha256": self.sha256_foo}
      }
      queue = list(artifacts.keys())
      self.assertListEqual(
          verify_match_rule(rule, queue, artifacts, self.links), ["foo"])

  def test_pass_match_material_star(self):
    """["MATCH", "foo*", "WITH", "MATERIALS", "FROM", "link-1"]],
    source artifacts foo* match destination materials foo* hashes, passes. """

    rule = ["MATCH", "foo*", "WITH", "MATERIALS", "FROM", "link-1"]
    artifacts = {
      "foo": {"sha256": self.sha256_foo},
      "foobar": {"sha256": self.sha256_foobar},
      "bar": {"sha256": self.sha256_bar}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["bar"])

  def test_pass_match_product_star(self):
    """["MATCH", "bar*", "WITH", "PRODUCTS", "FROM", "link-1"],
    source artifacts bar* match destination products bar* hashes, passes. """

    rule = ["MATCH", "bar*", "WITH", "PRODUCTS", "FROM", "link-1"]
    artifacts = {
      "bar": {"sha256": self.sha256_bar},
      "barfoo": {"sha256": self.sha256_barfoo},
      "foo": {"sha256": self.sha256_foo}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["foo"])

  def test_pass_match_star_in_source_dir_with_materials(self):
    """["MATCH", "foo*", "IN", "dist", "WITH", "MATERIALS", "FROM", "link-1"],
    source artifacts dist/* match destination materials foo* hashes, passes. """

    rule = ["MATCH", "foo*", "IN", "dist", "WITH", "MATERIALS", "FROM", "link-1"]
    artifacts = {
      "dist/foo": {"sha256": self.sha256_foo},
      "dist/foobar": {"sha256": self.sha256_foobar},
      "bar": {"sha256": self.sha256_bar}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["bar"])

  def test_pass_match_star_in_source_dir_with_products(self):
    """["MATCH", "bar*", "WITH", "PRODUCTS", "IN", "dist", "FROM", "link-1"],
    source artifacts dist/* match destination products bar* hashes, passes. """

    rule = ["MATCH", "bar*", "IN", "dist", "WITH", "PRODUCTS", "FROM", "link-1"]
    artifacts = {
      "dist/bar": {"sha256": self.sha256_bar},
      "dist/barfoo": {"sha256": self.sha256_barfoo},
      "foo": {"sha256": self.sha256_foo}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["foo"])

  def test_pass_match_star_in_with_materials_in_destination_dir(self):
    """["MATCH", "foo*", "WITH", "MATERIALS", "IN", "dist", "FROM", "link-1"],
    source artifacts foo* match destination materials dev/foo* hashes, passes. """

    rule = ["MATCH", "foo*", "WITH", "MATERIALS", "IN", "dev", "FROM", "link-1"]
    artifacts = {
      "foo": {"sha256": self.sha256_foo},
      "foobar": {"sha256": self.sha256_foobar},
      "bar": {"sha256": self.sha256_bar}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["bar"])

  def test_pass_match_star_with_products_destination_dir(self):
    """["MATCH", "bar*", "WITH", "PRODUCTS", "IN", "dev", "FROM", "link-1"],
    source artifacts bar* match destination products dev/bar* hashes, passes. """

    rule = ["MATCH", "bar*", "WITH", "PRODUCTS", "IN", "dev", "FROM", "link-1"]
    artifacts = {
      "bar": {"sha256": self.sha256_bar},
      "barfoo": {"sha256": self.sha256_barfoo},
      "foo": {"sha256": self.sha256_foo}
    }
    queue = list(artifacts.keys())
    self.assertListEqual(
        verify_match_rule(rule, queue, artifacts, self.links), ["foo"])

  def test_fail_destination_link_not_found(self):
    """["MATCH", "bar", "WITH", "MATERIALS", "FROM", "link-null"],
    destination link "link-null" not found, fails. """

    rule = ["MATCH", "bar", "WITH", "MATERIALS", "FROM", "link-null"]
    artifacts = {}
    queue = list(artifacts.keys())
    with self.assertRaises(RuleVerificationError):
      verify_match_rule(rule, queue, artifacts, self.links)

  def test_fail_path_not_in_destination_materials(self):
    """["MATCH", "bar", "WITH", "MATERIALS", "FROM", "link-1"]
    pattern bar does not match any materials in destination, fails. """

    rule = ["MATCH", "bar", "WITH", "MATERIALS", "FROM", "link-1"]
    artifacts = {
      "bar": {"sha256": self.sha256_bar},
    }
    queue = list(artifacts.keys())
    result = verify_match_rule(rule, queue, artifacts, self.links)
    self.assertEquals(['bar'], result);

  def test_fail_path_not_in_destination_products(self):
    """["MATCH", "foo", "WITH", "PRODUCTS", "FROM", "link-1"],
    pattern foo does not match any products in destination, fails. """

    rule = ["MATCH", "foo", "WITH", "PRODUCTS", "FROM", "link-1"]
    artifacts = {
      "foo": {"sha256": self.sha256_foo},
    }
    queue = list(artifacts.keys())
    result = verify_match_rule(rule, queue, artifacts, self.links)
    self.assertEquals(['foo'], result);

  def test_fail_hash_not_equal(self):
    """"["MATCH", "bar", "WITH", "PRODUCTS", "FROM", "link-1"],
    source and destination bar have different hashes, fails. """

    rule = ["MATCH", "bar", "WITH", "PRODUCTS", "FROM", "link-1"]
    artifacts = {
      "bar": {"sha256": "aaaaaaaaaa"},
    }
    queue = list(artifacts.keys())
    result = verify_match_rule(rule, queue, artifacts, self.links)
    self.assertEquals(['bar'], result);


class TestVerifyItemRules(unittest.TestCase):
  """Test verifylib.verify_item_rules(source_name, source_type, rules, links)"""

  def setUp(self):
    self.item_name = "item"
    self.sha256_1 = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"
    self.sha256_2 = \
        "cfdaaf1ab2e4661952a9dec5e8fa3c360c1b06b1a073e8493a7c46d2af8c504b"

    self.links = {
      "item": Metablock(signed=Link(name="item",
          materials={
              "foo": {"sha256": self.sha256_1},
              "foobar": {"sha256": self.sha256_1},
              "bar": {"sha256": self.sha256_1}
          },
          products={
              "baz" : {"sha256": self.sha256_1},
              "foo": {"sha256": self.sha256_1},
              "bar": {"sha256": self.sha256_2}
          }
      ))
    }

  def test_pass_material_rules_with_each_rule_type(self):
    """Pass with list of material rules of each rule type. """
    rules = [
      ["DELETE", "foobar"],
      ["CREATE", "baz"],
      ["MODIFY", "bar"],
      ["MATCH", "foo", "WITH", "MATERIALS", "FROM", "item"], # match with self
      ["DISALLOW", "barfoo"],
      ["ALLOW", "*"],
    ]
    verify_item_rules(self.item_name, "materials", rules, self.links)

  def test_pass_product_rules_with_each_rule_type(self):
    """Pass with list of material rules of each rule type. """
    rules = [
      ["DELETE", "foobar"],
      ["CREATE", "baz"],
      ["MODIFY", "bar"],
      ["MATCH", "foo", "WITH", "PRODUCTS", "FROM", "item"], # match with self
      ["DISALLOW", "barfoo"],
      ["ALLOW", "*"],
    ]
    verify_item_rules(self.item_name, "products", rules, self.links)

  def test_fail_wrong_source_type(self):
    """Fail with wrong source_type."""

    with self.assertRaises(securesystemslib.exceptions.FormatError):
      verify_item_rules(self.item_name, "artifacts", [], self.links)

  def test_pass_not_consumed_artifacts(self):
    """Pass with not consumed artifacts (implicit ALLOW *) """
    rules = []
    verify_item_rules(self.item_name, "materials", rules, self.links)


class TestVerifyAllItemRules(unittest.TestCase):
  """Test verifylib.verify_all_item_rules(items, links). """

  def setUp(self):
    """Create a dummy supply chain with two steps one inspection and the
    according link metadata:

    write-code (Step) ->  package (step) -> untar (Inspection)

    'write-code' creates an artifact foo
    'package' creates foo.tar.gz and deletes foo
    'untar' untars foo.tar.gz which results in foo.tar.gz and foo

    """

    self.sha256_foo = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"
    self.sha256_foo_tar = \
        "93c3c35a039a6a3d53e81c5dbee4ebb684de57b7c8be11b8739fd35804a0e918"

    self.steps = [
        Step(name="write-code",
            expected_products=[
                ["CREATE", "foo"]
            ],
        ),
        Step(name="package",
            expected_materials=[
                ["MATCH", "foo", "WITH", "PRODUCTS", "FROM", "write-code"]
            ],
            expected_products=[
                ["CREATE", "foo.tar.gz"],
                ["DELETE", "foo"]
            ],
        )
    ]

    self.inspections = [
        Inspection(name="untar",
            expected_materials=[
                ["MATCH", "foo.tar.gz", "WITH", "PRODUCTS", "FROM", "package"]
            ],
            expected_products=[
                ["MATCH", "foo", "IN", "dir", "WITH", "PRODUCTS",
                    "FROM", "write-code"]
            ]
        )
    ]

    self.links = {
      "write-code" : Metablock(signed=Link(name="write-code",
          products={
              "foo": {
                  "sha256": self.sha256_foo
              }
          }
      )),
      "package" : Metablock(signed=Link(name="package",
          materials={
              "foo": {
                  "sha256": self.sha256_foo
              }
          },
          products={
              "foo.tar.gz": {
                  "sha256": self.sha256_foo_tar
              }
          }
      )),
        "untar" : Metablock(signed=Link(name="untar",
            materials={
                "foo.tar.gz": {
                    "sha256": self.sha256_foo_tar
                }
            },
            products={
                "dir/foo": {
                    "sha256": self.sha256_foo
                },
            }
        ))
    }

  def test_pass_verify_all_step_rules(self):
    """Pass rule verification for dummy supply chain Steps. """
    verify_all_item_rules(self.steps, self.links)

  def test_pass_verify_all_inspection_rules(self):
    """Pass rule verification for dummy supply chain Inspections. """
    verify_all_item_rules(self.inspections, self.links)


class TestInTotoVerify(unittest.TestCase):
  """
  Tests verifylib.in_toto_verify(layout_path, layout_key_paths).

  Uses in-toto demo supply chain link metadata files and basic layout for
  verification.

  Copies the basic layout for different test scenarios:
    - single-signed layout
    - double-signed layout
    - expired layout
    - layout with failing link rule
    - layout with failing step rule

  """
  @classmethod
  def setUpClass(self):
    """Creates and changes into temporary directory.
    Copies demo files to temp dir...
      - owner/functionary key pairs
      - *.link metadata files
      - layout template (not signed, no expiration date)
      - final product

    ...and dumps various layouts for different test scenarios
    """
    # Backup original cwd
    self.working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # Create and change into temporary directory
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)

    # Copy demo files to temp dir
    for file in os.listdir(demo_files):
      shutil.copy(os.path.join(demo_files, file), self.test_dir)

    # Load layout template
    layout_template = Metablock.load("demo.layout.template")

    # Store various layout paths to be used in tests
    self.layout_single_signed_path = "single-signed.layout"
    self.layout_double_signed_path = "double-signed.layout"
    self.layout_bad_sig = "bad-sig.layout"
    self.layout_expired_path = "expired.layout"
    self.layout_failing_step_rule_path = "failing-step-rule.layout"
    self.layout_failing_inspection_rule_path = "failing-inspection-rule.layout"
    self.layout_failing_inspection_retval = "failing-inspection-retval.layout"
    self.layout_no_steps_no_inspections = "no_steps_no_inspections.layout"


    # Import layout signing keys
    alice = import_rsa_key_from_file("alice")
    bob = import_rsa_key_from_file("bob")
    self.alice_path = "alice.pub"
    self.bob_path = "bob.pub"

    # dump single signed layout
    layout = copy.deepcopy(layout_template)
    layout.sign(alice)
    layout.dump(self.layout_single_signed_path)

    # dump double signed layout
    layout = copy.deepcopy(layout_template)
    layout.sign(alice)
    layout.sign(bob)
    layout.dump(self.layout_double_signed_path)

    # dump layout with bad signature
    layout = copy.deepcopy(layout_template)
    layout.sign(alice)
    layout.signed.readme = "this breaks the signature"
    layout.dump(self.layout_bad_sig)

    # dump expired layout
    layout = copy.deepcopy(layout_template)
    layout.signed.expires = (datetime.today() +
        relativedelta(months=-1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    layout.sign(alice)
    layout.dump(self.layout_expired_path)

    # dump layout with failing step rule
    layout = copy.deepcopy(layout_template)
    layout.signed.steps[0].expected_products.insert(0,
        ["DISALLOW", "*"])
    layout.signed.steps[0].expected_products.insert(0,
        ["MODIFY", "*"])
    layout.sign(alice)
    layout.dump(self.layout_failing_step_rule_path)

    # dump layout with failing inspection rule
    layout = copy.deepcopy(layout_template)
    layout.signed.inspect[0].expected_materials.insert(0,
        ["MODIFY", "*"])
    layout.signed.inspect[0].expected_materials.append(
        ["DISALLOW", "*"])
    layout.sign(alice)
    layout.dump(self.layout_failing_inspection_rule_path)

    # dump layout with failing inspection retval
    layout = copy.deepcopy(layout_template)
    layout.signed.inspect[0].run = ["expr",  "1", "/", "0"]
    layout.sign(alice)
    layout.dump(self.layout_failing_inspection_retval)

    # dump empty layout
    layout = Metablock(signed=Layout())
    layout.sign(alice)
    layout.dump(self.layout_no_steps_no_inspections)
    self.alice = alice

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp dir. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_verify_passing(self):
    """Test pass verification of single-signed layout. """
    layout = Metablock.load(self.layout_single_signed_path)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path])
    in_toto_verify(layout, layout_key_dict)

  def test_verify_passing_double_signed_layout(self):
    """Test pass verification of double-signed layout. """
    layout = Metablock.load(self.layout_double_signed_path)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path, self.bob_path])
    in_toto_verify(layout, layout_key_dict)

  def test_verify_passing_empty_layout(self):
    """Test pass verification of layout without steps or inspections. """
    layout = Metablock.load(self.layout_no_steps_no_inspections)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict(
        [self.alice_path])
    in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_wrong_key(self):
    """Test fail verification with wrong layout key. """
    layout = Metablock.load(self.layout_single_signed_path)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.bob_path])
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_bad_signature(self):
    """Test fail verification with bad layout signature. """
    layout = Metablock.load(self.layout_bad_sig)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_layout_expired(self):
    """Test fail verification with expired layout. """
    layout = Metablock.load(self.layout_expired_path)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(LayoutExpiredError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_link_metadata_files(self):
    """Test fail verification with link metadata files not found. """
    os.rename("package.2f89b927.link", "package.link.bak")
    layout = Metablock.load(self.layout_single_signed_path)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(in_toto.exceptions.LinkNotFoundError):
      in_toto_verify(layout, layout_key_dict)
    os.rename("package.link.bak", "package.2f89b927.link")

  def test_verify_failing_inspection_exits_non_zero(self):
    """Test fail verification with inspection returning non-zero. """
    layout = Metablock.load(self.layout_failing_inspection_retval)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(BadReturnValueError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_step_rules(self):
    """Test fail verification with failing step artifact rule. """
    layout = Metablock.load(self.layout_failing_step_rule_path)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(RuleVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_inspection_rules(self):
    """Test fail verification with failing inspection artifact rule. """
    layout = Metablock.load(self.layout_failing_inspection_rule_path)
    layout_key_dict = import_rsa_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(RuleVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_layout_signatures_fail_with_no_keys(self):
    """Layout signature verification fails when no keys are passed. """
    layout_metablock = Metablock(signed=Layout())
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout_metablock, {})

  def test_verify_layout_signatures_fail_with_malformed_signature(self):
    """Layout signature verification fails with malformed signatures. """
    layout_metablock = Metablock(signed=Layout())
    signature = layout_metablock.sign(self.alice)
    pubkey = self.alice
    pubkey["keyval"]["private"] = ""

    del signature["sig"]
    layout_metablock.signed.signatures = [signature]
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout_metablock, {self.alice["keyid"]: pubkey})





class TestInTotoVerifyThresholds(unittest.TestCase):
  """Test verifylib functions related to signature thresholds.

    - verifylib.verify_link_signature_thresholds
    - verifylib.verify_threshold_constraints """


  @classmethod
  def setUpClass(self):
    """Load test keys from demo files. """
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    self.alice = import_rsa_key_from_file(
        os.path.join(demo_files, "alice"))
    self.alice_pubkey = import_rsa_key_from_file(
        os.path.join(demo_files, "alice.pub"))
    self.alice_keyid = self.alice["keyid"]

    self.bob = import_rsa_key_from_file(
        os.path.join(demo_files, "bob"))
    self.bob_pubkey = import_rsa_key_from_file(
        os.path.join(demo_files, "bob.pub"))
    self.bob_keyid = self.bob["keyid"]

    self.name = "test"
    self.foo_hash = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"


  def test_thresholds_skip_unauthorized_links(self):
    """Ignore links with unauthorized signatures. """
    # Layout with one step, one authorized functionary and threshold 1
    layout = Layout(
        keys={
          self.bob_keyid: self.bob_pubkey
        },
        steps=[
          Step(
            name=self.name,
            pubkeys=[self.bob_keyid])
          ]
      )

    # Signed links (one authorized the other one not)
    link_bob = Metablock(signed=Link(name=self.name))
    link_bob.sign(self.bob)
    link_alice = Metablock(signed=Link(name=self.name))
    link_alice.sign(self.alice)

    # The dictionary of links per step passed to the verify function
    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice
      }
    }

    # The dictionary of links expected to be returned, not containing the
    # unauthorized link, but enough (threshold) authorized links
    expected_chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob
      }
    }
    # Verify signatures/thresholds
    returned_chain_link_dict = verify_link_signature_thresholds(
        layout, chain_link_dict)
    # Test that the returned dict is as expected
    self.assertDictEqual(returned_chain_link_dict, expected_chain_link_dict)


  def test_thresholds_skip_links_with_failing_signature(self):
    """Ignore links with failing signatures. """

    # Layout with one step, two authorized functionaries and threshold 1
    layout = Layout(
        keys={
          self.bob_keyid: self.bob_pubkey,
          self.alice_keyid: self.alice_pubkey,
        },
        steps=[
          Step(
            name=self.name,
            pubkeys=[self.bob_keyid, self.alice_keyid],
            threshold=1)
          ]
        )

    # Authorized links (one signed one not)
    link_bob = Metablock(signed=Link(name=self.name))
    link_bob.sign(self.bob)
    link_alice = Metablock(signed=Link(name=self.name))

    # The dictionary of links per step passed to the verify function
    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice
      }
    }

    # The dictionary of links expected to be returned, not containing the
    # unauthorized link, but enough (threshold) authorized links
    expected_chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob
      }
    }

    # Verify signatures/thresholds
    returned_chain_link_dict = verify_link_signature_thresholds(
        layout, chain_link_dict)
    # Test that the returned dict is as expected
    self.assertDictEqual(returned_chain_link_dict, expected_chain_link_dict)


  def test_thresholds_fail_with_not_enough_valid_links(self):
    """ Fail with not enough authorized links. """

    # Layout with one step, two authorized functionaries and threshold 2
    layout = Layout(
        keys={
          self.bob_keyid: self.bob_pubkey,
          self.alice_keyid: self.alice_pubkey,
        },
        steps=[
          Step(
            name=self.name,
            pubkeys=[self.bob_keyid, self.alice_keyid],
            threshold=2)
          ]
        )

    # Only one authorized and validly signed link
    link_bob = Metablock(signed=Link(name=self.name))
    link_bob.sign(self.bob)

    # The dictionary of links per step passed to the verify function
    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob
      }
    }

    # Fail signature threshold verification with not enough links
    with self.assertRaises(ThresholdVerificationError):
      verify_link_signature_thresholds(layout, chain_link_dict)


  def test_threshold_constraints_fail_with_not_enough_links(self):
    """ Fail with not enough links. """
    # Layout with one step and threshold 2
    layout = Layout(steps=[Step(name=self.name, threshold=2)])
    # Authorized (unsigned) link
    # This function does not care for signatures it just verifies if the
    # different links have recorded the same artifacts. Signature verification
    # happens earlier in the final product verification (see tests above)
    link_bob = Metablock(signed=Link(name=self.name))

    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
      }
    }

    with self.assertRaises(ThresholdVerificationError):
      verify_threshold_constraints(layout, chain_link_dict)


  def test_threshold_constraints_fail_with_unequal_links(self):
    """ Test that the links for a step recorded the same artifacts. """
    # Layout with one step and threshold 2
    layout = Layout(steps=[Step(name=self.name, threshold=2)])
    link_bob = Metablock(
        signed=Link(
          name=self.name,
          materials={
            "foo": { "sha256": self.foo_hash}
          }
        )
      )
    # Cf. signing comment in test_thresholds_constraints_with_not_enough_links
    link_alice = Metablock(signed=Link(name=self.name))

    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice,
      }
    }

    with self.assertRaises(ThresholdVerificationError):
      verify_threshold_constraints(layout, chain_link_dict)



  def test_threshold_constraints_pas_with_equal_links(self):
    """ Pass threshold constraint verification with equal links. """
    # Layout with one step and threshold 2
    layout = Layout(steps=[Step(name=self.name, threshold=2)])
    # Two authorized links with equal artifact recordings (materials)
    # Cf. signing comment in test_thresholds_constraints_with_not_enough_links
    link_bob = Metablock(
        signed=Link(
          name=self.name,
          materials={
            "foo": { "sha256": self.foo_hash}
          }
        )
      )
    link_alice = Metablock(
        signed=Link(
          name=self.name,
          materials={
            "foo": { "sha256": self.foo_hash}
          }
        )
      )

    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice,
      }
    }

    verify_threshold_constraints(layout, chain_link_dict)





class TestInTotoVerifyThresholdsGpgSubkeys(unittest.TestCase):
  """
  Test the following 8 scenarios for combinations of link authorization,
  where a link is either signed by a master or subkey (SIG), and the
  corresponding step authorizes either the master or subkey (AUTH), and the
  corresponding top level key in the layout key store is either a master key
  (bundle, i.e. with subkeys) or a subkey (KEY).

  M ... Masterkey
  S ... Subkey

  SIG AUTH KEY(bundle)| OK  | Comment
  ---------------------------------------------------------------
  M   M    M          | Yes | Normal scenario (*)
  M   M    S          | No  | Cannot find key in key store + cannot sign (*)
  M   S    M          | No  | Unallowed trust delegation + cannot sign (*)
  M   S    S          | No  | Unallowed trust delegation + cannot sign (*)
  S   M    M          | Yes | Allowed trust delegation
  S   M    S          | No  | Cannot associate keys
  S   S    M          | Yes | Can find key in key store
  S   S    S          | Yes | Generalizes to normal scenario

  (*) NOTE: Master keys with a subkey with signing capability always use that
  subkey, even if the master keyid is specified and has signing capability.

  """

  @classmethod
  def setUpClass(self):
    # Create directory to run the tests without having everything blow up
    self.working_dir = os.getcwd()

    # Find demo files
    gpg_keyring_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "gpg_keyrings", "rsa")

    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    self.gnupg_home = os.path.join(self.test_dir, "rsa")
    shutil.copytree(gpg_keyring_path, self.gnupg_home)

    self.master = "8465a1e2e0fb2b40adb2478e18fb3f537e0c8a17"
    self.sub = "c5a0abe6ec19d0d65f85e2c39be9df5131d924e9"

    master_key = in_toto.gpg.functions.gpg_export_pubkey(
        self.master, self.gnupg_home)
    sub_key = master_key["subkeys"][self.sub]

    # We need a gpg key without subkeys to test the normal scenario (M M M),
    # because keys with signing subkeys always use that subkey for signing.
    self.master2 = "7B3ABB26B97B655AB9296BD15B0BD02E1C768C43"
    master_key2 = in_toto.gpg.functions.gpg_export_pubkey(
        self.master2, self.gnupg_home)


    self.pub_key_dict = {
      self.master: master_key,
      self.sub: sub_key,
      self.master2: master_key2
    }

    self.step_name = "name"

    os.chdir(self.test_dir)


  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp test directory. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def _verify_link_signature_tresholds(self, sig_id, auth_id, key_id):
    metablock = Metablock(signed=Link(name=self.step_name))
    metablock.sign_gpg(sig_id, self.gnupg_home)                        # SIG

    chain_link_dict = {
      self.step_name : {
        sig_id : metablock                                             # SIG
      }
    }

    layout = Layout(
      steps=[
        Step(
            name=self.step_name,
            pubkeys=[auth_id]                                          # AUTH
          )
        ],
      keys={
          key_id: self.pub_key_dict[key_id]                            # KEY
        }
      )
    return layout, chain_link_dict


  def test_verify_link_signature_thresholds__M_M_M(self):
    """Normal scenario. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.master2, self.master2, self.master2)

    verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__M_M_S__M_S_M__M_S_S(self):
    """Cannot sign with master key if subkey is present. """
    # The scenarios MMS, MSM, MSS are impossible because we cannot sign
    # with a master key, if there is a subkey with signing capability
    # GPG will always use that subkey.
    # Even if gpg would use the masterkey, these scenarios are not allowed,
    # see table in docstring of testcase
    signature = in_toto.gpg.functions.gpg_sign_object(
        b"data", self.master, self.gnupg_home)

    self.assertTrue(signature["keyid"] == self.sub)


  def test_verify_link_signature_thresholds__S_M_M(self):
    """Allowed trust delegation. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.master, self.master)
    verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__S_M_S(self):
    """Cannot associate keys. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.master, self.sub)
    with self.assertRaises(ThresholdVerificationError):
      verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__S_S_M(self):
    """No trust delegation and can find key in key store. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.sub, self.master)
    verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__S_S_S(self):
    """Generalizes to normal scenario. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.sub, self.sub)
    verify_link_signature_thresholds(layout, chain_link_dict)





class TestVerifySublayouts(unittest.TestCase):
  """Tests verifylib.verify_sublayouts(layout, reduced_chain_link_dict).
  Call with one-step super layout that has a sublayout (demo layout). """

  @classmethod
  def setUpClass(self):
    """Creates and changes into temporary directory and prepares two layouts.
    The superlayout, which has one step and its sublayout, which is the usual
    demo layout (write code, package, inspect tar). """

    # Backup original cwd
    self.working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # Create and change into temporary directory
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)

    # Copy demo files to temp dir
    for file in os.listdir(demo_files):
      shutil.copy(os.path.join(demo_files, file), self.test_dir)

    # Import sub layout signing (private) and verifying (public) keys
    alice = import_rsa_key_from_file("alice")
    alice_pub = import_rsa_key_from_file("alice.pub")

    # From the perspective of the superlayout, the sublayout is treated as
    # a link corresponding to a step, hence needs a name.
    sub_layout_name = "sub_layout"

    # Sublayout links are expected in a directory relative to the superlayout's
    # link directory
    sub_layout_link_dir = SUBLAYOUT_LINK_DIR_FORMAT.format(
        name=sub_layout_name, keyid=alice["keyid"])

    for sublayout_link_name in glob.glob("*.link"):
      dest_path = os.path.join(sub_layout_link_dir, sublayout_link_name)
      os.renames(sublayout_link_name, dest_path)


    # Copy, sign and dump sub layout as link from template
    layout_template = Metablock.load("demo.layout.template")
    sub_layout = copy.deepcopy(layout_template)
    sub_layout_path = FILENAME_FORMAT.format(step_name=sub_layout_name,
        keyid=alice_pub["keyid"])
    sub_layout.sign(alice)
    sub_layout.dump(sub_layout_path)

    # Create super layout that has only one step, the sublayout
    self.super_layout = Layout()
    self.super_layout.keys[alice_pub["keyid"]] = alice_pub
    sub_layout_step = Step(
        name=sub_layout_name,
        pubkeys=[alice_pub["keyid"]]
      )
    self.super_layout.steps.append(sub_layout_step)

    # Load the super layout links (i.e. the sublayout)
    self.super_layout_links = load_links_for_layout(self.super_layout, ".")

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp dir. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_verify_demo_as_sublayout(self):
    """Test super layout's passing sublayout verification. """
    verify_sublayouts(
        self.super_layout, self.super_layout_links, ".")





class TestInTotoVerifyMultiLevelSublayouts(unittest.TestCase):
  """Test verifylib.in_toto_verify with multiple levels of sublayouts. """

  def test_verify_multi_level_sublayout(self):
    # Backup original cwd
    working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # Create and change into temporary directory
    test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(test_dir)

    # We don't need to copy the demo files, we just load the keys
    keys = {}
    for key_name in ["alice", "bob", "carl"]:
      keys[key_name + "_priv"] = import_rsa_key_from_file(
          os.path.join(demo_files, key_name))
      keys[key_name + "_pub"] = import_rsa_key_from_file(
          os.path.join(demo_files, key_name + ".pub"))


    # Create layout hierarchy

    # Root layout
    # The root layout is the layout that will be passed to `in_toto_verify`
    # It only has one step which is a sublayout, into which verification
    # recurses. Only the root layout and root layout verification key will be
    # passed to verification.
    root_layout_pub_key_dict = {
        keys["alice_pub"]["keyid"]: keys["alice_pub"]
      }

    root_layout_name = "root.layout"
    root_layout_step_name = "delegated-to-bob"

    root_layout = Metablock(signed=Layout(
        keys={
          keys["bob_pub"]["keyid"]: keys["bob_pub"]
        },
        steps=[
            Step(
              name=root_layout_step_name,
              pubkeys=[
                keys["bob_pub"]["keyid"]
              ]
            )
          ]
        )
      )
    root_layout.sign(keys["alice_priv"])


    # Sublayout (first level)
    # The first level sublayout wil be treated as a link from the
    # superlayout's perspective and loaded from the current working directory.
    # The link for the only step of this sublayout will be placed in a
    # namespaced subdir, that link itself is a sublayout (subsublayout).
    bobs_layout_name = FILENAME_FORMAT.format(
        step_name=root_layout_step_name,
        keyid=keys["bob_pub"]["keyid"])

    bobs_layout_link_dir = SUBLAYOUT_LINK_DIR_FORMAT.format(
        name=root_layout_step_name,
        keyid=keys["bob_pub"]["keyid"])
    os.mkdir(bobs_layout_link_dir)

    bobs_layout_step_name = "delegated-to-carl"

    bobs_layout = Metablock(signed=Layout(
        keys={
          keys["carl_pub"]["keyid"]: keys["carl_pub"]
          },
        steps=[
            Step(
              name=bobs_layout_step_name,
              pubkeys=[keys["carl_pub"]["keyid"]]
            )
          ]
        )
      )
    bobs_layout.sign(keys["bob_priv"])
    bobs_layout.dump(bobs_layout_name)


    # Subsublayout (second level)
    # The subsublayout will be placed in the namespaced link dir
    # of its superlayout (sublayout from the root layout's perspective), for
    # for which it serves as link.
    carls_layout_name = FILENAME_FORMAT.format(
            step_name=bobs_layout_step_name,
            keyid=keys["carl_pub"]["keyid"])

    carls_layout_path = os.path.join(bobs_layout_link_dir, carls_layout_name)
    carls_layout = Metablock(signed=Layout())
    carls_layout.sign(keys["carl_priv"])
    carls_layout.dump(carls_layout_path)

    in_toto_verify(root_layout, root_layout_pub_key_dict)

    os.chdir(working_dir)
    shutil.rmtree(test_dir)





class TestGetSummaryLink(unittest.TestCase):
  """Tests verifylib.get_summary_link(layout, reduced_chain_link_dict).
  Pass two step demo layout and according link files and verify the
  returned summary link.
  """

  @classmethod
  def setUpClass(self):
    """Creates and changes into temporary directory and prepares two layouts.
    The superlayout, which has one step and its sublayout, which is the usual
    demo layout (write code, package, inspect tar). """

    # Backup original cwd
    self.working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # Create and change into temporary directory
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)

    # Copy demo files to temp dir
    for file in os.listdir(demo_files):
      shutil.copy(os.path.join(demo_files, file), self.test_dir)

    self.demo_layout = Metablock.load("demo.layout.template")
    self.code_link = Metablock.load("package.2f89b927.link")
    self.package_link = Metablock.load("write-code.776a00e2.link")
    self.demo_links = {
        "write-code": self.code_link,
        "package": self.package_link
      }

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp dir. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_get_summary_link_from_demo_layout(self):
    """Create summary link from demo link files and compare properties. """
    sum_link = get_summary_link(self.demo_layout.signed, self.demo_links)

    self.assertEquals(sum_link.signed._type, self.code_link.signed._type)
    self.assertEquals(sum_link.signed.name, self.code_link.signed.name)
    self.assertEquals(sum_link.signed.materials, self.code_link.signed.materials)

    self.assertEquals(sum_link.signed.products, self.package_link.signed.products)
    self.assertEquals(sum_link.signed.command, self.package_link.signed.command)
    self.assertEquals(sum_link.signed.byproducts, self.package_link.signed.byproducts)
    self.assertEquals(sum_link.signed.byproducts.get("return-value"),
        self.package_link.signed.byproducts.get("return-value"))



if __name__ == "__main__":
  unittest.main()
