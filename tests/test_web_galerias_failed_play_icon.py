"""A busy Telegram account is not a corrupt video."""
import unittest
from web_galerias.on_demand import _result

class FailureClassification(unittest.TestCase):
 def setUp(self):
  class Cipher:
   def encrypt(self,data): return b"encrypted"
  class Service:
   cipher=Cipher()
  class G:
   s=Service()
  self.g=G()
  self.row={"id":75,"kind":"video","mime":"video/mp4",
            "size":104857600,"state":"preview","thumb_key":"f/x","file_key":None}
 def test_running_or_waiting_never_hides_play_icon(self):
  for last_error in ("","PauseRequested","FloodWaitError"):
   with self.subTest(last_error=last_error):
    p={"state":"queued","next_attempt":0,"received_bytes":0,"expected_bytes":0,
       "last_error":last_error,"attempts":3}
    d=_result(self.g,100,self.row,p)
    self.assertFalse(d["priority_failed"])
 def test_only_proven_invalid_original_is_hidden(self):
  p={"state":"queued","next_attempt":0,"received_bytes":0,"expected_bytes":0,
     "last_error":"invalid_or_incomplete_original","attempts":1}
  d=_result(self.g,100,self.row,p)
  self.assertTrue(d["priority_failed"])
  self.assertEqual(d["size"],104857600)
