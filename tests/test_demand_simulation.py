import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('demand',Path(__file__).resolve().parents[1]/'simulations'/'demand.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
Simulation=module.Simulation
class DemandModel(unittest.TestCase):
    def test_shared_pacing_limits_even_unlimited_preparation(self):
        result=Simulation([1]*1000,1000,0,ack=False,completion=False,archives=False).run()
        self.assertGreaterEqual(result['first_delivery_from_user_request']['max'],999*.06)
    def test_current_six_slots_fail_the_sixty_second_target(self):
        result=Simulation([1]*1000,6,30).run()
        self.assertGreater(result['first_start_from_user_request']['max'],5000)
    def test_optimized_start_is_not_the_same_as_delivery(self):
        result=Simulation([1]*1000,600,30,ack=False,completion=False,fair=True).run()
        self.assertLess(result['first_start_from_user_request']['max'],60)
        self.assertGreater(result['first_delivery_from_user_request']['max'],60)
