"""Public gallery admin cards must not accumulate dead Telegram metadata."""
import unittest
from web_galerias.folder_sources import combine_admin_rows


def source(fid,peer,connected,*,ready=0,photos=0,videos=0,
           pending=0,missing=0,scan=0,telegram_photos=None,
           telegram_videos=None,error="",uid=101):
    return dict(id=fid,peer=peer,source_enabled=connected,
                preferred_uid=uid if connected else None,
                source_uid=uid if connected else None,
                last_source_uid=uid if not connected else None,
                source_access_status="ok" if connected else "disconnected",
                scan_count=scan,ready=ready,r2_photos=photos,r2_videos=videos,
                visible_photos=photos,visible_videos=videos,
                pending=pending,missing=missing,media_total=ready+pending,
                telegram_photos_total=telegram_photos,
                telegram_videos_total=telegram_videos,
                last_check=95,telegram_counts_at=95,
                automatic_cover=fid,error=error,complete=connected)


class CleanDisconnectedSources(unittest.TestCase):
    def setUp(self):
        self.rows=[
            source(1,-100111,False,ready=14,photos=8,videos=6,
                   pending=900,missing=400,scan=30000,
                   telegram_photos=17000,telegram_videos=12000,
                   error="Esta cuenta vieja se desconectó",uid=111),
            source(2,-100222,False,ready=25,photos=15,videos=10,
                   pending=500,missing=100,scan=8000,
                   telegram_photos=5000,telegram_videos=4000,
                   error="SESSION_REVOKED",uid=-222),
            source(3,-100333,True,ready=4,photos=3,videos=1,
                   pending=3,missing=1,scan=42,
                   telegram_photos=400,telegram_videos=200,
                   error="",uid=333)]
        self.links=[dict(parent_folder=1,source_folder=2),
                    dict(parent_folder=1,source_folder=3)]

    def test_only_live_group_and_account_are_displayed(self):
        result=combine_admin_rows(self.rows,self.links)
        self.assertEqual(len(result),1)
        folder=result[0]
        self.assertEqual(folder["active_source_count"],1)
        self.assertEqual(len(folder["sources"]),1)
        self.assertEqual(folder["sources"][0]["peer"],-100333)
        self.assertEqual(folder["sources"][0]["account_uid"],333)
        self.assertNotIn("last_account_uid",folder["sources"][0])
        self.assertNotIn("SESSION_REVOKED",folder["error"])
        self.assertEqual(folder["scan_count"],42)
        self.assertEqual(folder["telegram_photos_total"],400)
        self.assertEqual(folder["telegram_videos_total"],200)
        self.assertEqual(folder["pending"],3)
        self.assertEqual(folder["missing"],1)
        self.assertEqual(folder["media_total"],7)
        self.assertEqual(folder["ready"],43)
        self.assertEqual(folder["r2_photos"],26)
        self.assertEqual(folder["r2_videos"],17)

    def test_no_live_sources_shows_cloud_only_and_no_obsolete_totals(self):
        for row in self.rows:
            row["source_enabled"]=False
            row["source_access_status"]="disconnected"
        folder=combine_admin_rows(self.rows,self.links)[0]
        self.assertEqual(folder["sources"],[])
        self.assertEqual(folder["active_source_count"],0)
        self.assertEqual(folder["ready"],43)
        self.assertEqual(folder["r2_photos"],26)
        self.assertEqual(folder["r2_videos"],17)
        self.assertEqual(folder["scan_count"],0)
        self.assertEqual(folder["pending"],0)
        self.assertEqual(folder["missing"],0)
        self.assertEqual(folder["media_total"],0)
        self.assertIsNone(folder["telegram_photos_total"])
        self.assertIsNone(folder["telegram_videos_total"])
        self.assertEqual(folder["telegram_counts_at"],0)
        self.assertEqual(folder["last_check"],0)
        self.assertEqual(folder["error"],"")


if __name__=="__main__":
    unittest.main()
