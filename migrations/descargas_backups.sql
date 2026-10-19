-- Apply to LADRON's Supabase project, not DESCARGAS's queue database.
CREATE TABLE IF NOT EXISTS public.descargas_backups (
 media_key text NOT NULL, item_index integer NOT NULL CHECK(item_index BETWEEN 0 AND 9),
 bot_id bigint NOT NULL, telegram_id bigint NOT NULL, file_id text NOT NULL,
 size_bytes bigint NOT NULL CHECK(size_bytes>=0), dropbox_path text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done')),
 content_hash text,error_type text,created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(media_key,item_index));
ALTER TABLE public.descargas_backups ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.descargas_backups FROM anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON TABLE public.descargas_backups TO service_role;
