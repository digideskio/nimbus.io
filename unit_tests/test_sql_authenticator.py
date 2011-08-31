# -*- coding: utf-8 -*-
"""
test_sql_authenticator.py

test the customer authentication process
as a part of the test, it give the customer rputines a good workout too
"""
import unittest

from diyapi_tools.standard_logging import initialize_logging
from diyapi_tools.database_connection import get_central_connection
from diyapi_tools.customer import purge_customer, \
        create_customer, \
        add_key_to_customer

from diyapi_web_server.sql_authenticator import SqlAuthenticator

class MockRequest(object):
    pass

_log_path = "/var/log/pandora/test_sql_authenticator.log"
_cluster_name = "multi-node-cluster"
_local_node_name = "multi-node-01"
_test_username = "test-sqlauthenticator-user"

class TestSqlAuthenticator(unittest.TestCase):
    """test the customer authentication process"""

    def setUp(self):
        self.tearDown()
        self._connection = get_central_connection()
        self._connection.execute("begin")
        create_customer(self._connection, _test_username)
        add_key_to_customer(self._connection, _test_username)
        self._connection.commit()

    def tearDown(self):
        if hasattr(self, "_connection") \
        and self._connection is not None:
            self._connection.execute("begin")
            purge_customer(self._connection, _test_username)
            self._connection.commit()
            self._connection.close()
            self._connection = None

    def test_valid_customer(self):
        """test a customer who should authenticate successfully"""
        self.assertTrue(False)

if __name__ == "__main__":
    initialize_logging(_log_path)
    unittest.main()

