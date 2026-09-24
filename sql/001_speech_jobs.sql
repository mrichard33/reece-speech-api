-- Reece Speech API — job storage. Only needed when JOB_STORE=supabase.
-- Run on the LP MCP Supabase instance, in the dashboard SQL editor.
-- Run the THREE statements below as three SEPARATE executions, in this order.
-- (Step 3 uses CREATE INDEX CONCURRENTLY, which cannot run inside a multi-statement batch.)
-- No audio is ever stored here — only job status and transcript text.

-- ---------------------------------------------------------------- 1 of 3
create table if not exists speech_jobs (
  id uuid primary key default gen_random_uuid(),
  status text not null default 'queued'
    check (status in ('queued','processing','done','failed')),
  source_name text,
  duration_seconds numeric,
  language text,
  model text,
  chunks_total int,
  chunks_done int not null default 0,
  text text,
  segments jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

-- ---------------------------------------------------------------- 2 of 3
create table if not exists speech_job_chunks (
  job_id uuid not null references speech_jobs(id) on delete cascade,
  chunk_index int not null,
  start_seconds numeric not null,
  end_seconds numeric not null,
  status text not null default 'queued'
    check (status in ('queued','processing','done','failed')),
  text text,
  segments jsonb,
  attempts int not null default 0,
  error text,
  primary key (job_id, chunk_index)
);

-- ---------------------------------------------------------------- 3 of 3
create index concurrently if not exists idx_speech_jobs_status_created
  on speech_jobs (status, created_at);
