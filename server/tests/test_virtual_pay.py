import hashlib
import hmac
import json
import unittest

from app.services.virtual_pay import calc_pay_sig, calc_user_signature, sign_data


class VirtualPayTests(unittest.TestCase):
    def test_signatures_match_document_algorithm(self):
        body = '{"offerId":"1450648016","buyQuantity":1}'
        expected = hmac.new(b'key', ('requestVirtualPayment&' + body).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(calc_pay_sig('requestVirtualPayment', body, 'key'), expected)
        expected_user = hmac.new(b'session', body.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(calc_user_signature(body, 'session'), expected_user)

    def test_sign_data_is_compact_and_stable(self):
        body = sign_data(offer_id='1450648016', quantity=1, env=0, product_id='book.pro', goods_price=990, out_trade_no='LW20260913123456ABC', attach='{"plan":"pro_monthly"}')
        value = json.loads(body)
        self.assertEqual(value['currencyType'], 'CNY')
        self.assertEqual(list(value), ['offerId', 'buyQuantity', 'env', 'currencyType', 'productId', 'goodsPrice', 'outTradeNo', 'attach'])
        self.assertNotIn(' ', body)


if __name__ == '__main__':
    unittest.main()
