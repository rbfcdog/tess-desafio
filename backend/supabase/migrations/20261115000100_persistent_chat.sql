begin;

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create table public.user_settings (
  user_id uuid primary key references public.profiles(id) on delete cascade,
  system_prompt text not null default '' check (length(system_prompt) <= 8000)
);
create table public.conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  title text not null check (length(btrim(title)) between 1 and 120),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);
create index conversations_owner_updated on public.conversations(user_id, updated_at desc);
create table public.runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  conversation_id uuid not null,
  request_id uuid not null,
  model text not null,
  status text not null default 'running' check (status in ('running','completed','failed','interrupted')),
  reserved_tokens bigint not null check (reserved_tokens > 0),
  input_tokens bigint check (input_tokens >= 0),
  output_tokens bigint check (output_tokens >= 0),
  error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz,
  unique (user_id, request_id),
  unique (id, user_id, conversation_id),
  foreign key (conversation_id, user_id) references public.conversations(id, user_id) on delete cascade
);
create unique index one_running_run_per_conversation on public.runs(conversation_id) where status = 'running';
create index runs_user_created on public.runs(user_id, created_at desc);
create table public.messages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  conversation_id uuid not null,
  run_id uuid not null,
  role text not null check (role in ('user','assistant')),
  content text not null default '',
  status text not null check (status in ('running','completed','failed','interrupted')),
  created_at timestamptz not null default clock_timestamp(),
  unique (run_id, role),
  foreign key (run_id, user_id, conversation_id) references public.runs(id, user_id, conversation_id) on delete cascade
);
create index messages_conversation_order on public.messages(conversation_id, created_at, id);
create table public.usage_accounts (
  id text primary key check (id = 'global'),
  limit_tokens bigint not null check (limit_tokens > 0),
  used_tokens bigint not null default 0 check (used_tokens >= 0),
  reserved_tokens bigint not null default 0 check (reserved_tokens >= 0)
);
insert into public.usage_accounts(id, limit_tokens) values ('global', 1000000);
create table public.usage_ledger (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  run_id uuid not null unique references public.runs(id) on delete cascade,
  charged_tokens bigint not null check (charged_tokens >= 0),
  input_tokens bigint check (input_tokens >= 0),
  output_tokens bigint check (output_tokens >= 0),
  usage_observed boolean not null,
  created_at timestamptz not null default now()
);
create index usage_ledger_owner on public.usage_ledger(user_id);
create table public.audit_events (
  id bigint generated always as identity primary key,
  user_id uuid references public.profiles(id) on delete set null,
  conversation_id uuid references public.conversations(id) on delete set null,
  run_id uuid references public.runs(id) on delete set null,
  event text not null,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index audit_events_owner_created on public.audit_events(user_id, created_at desc);

create function public.initialize_chat_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles(id) values (new.id);
  insert into public.user_settings(user_id) values (new.id);
  insert into public.audit_events(user_id,event) values (new.id,'user.created');
  return new;
end;
$$;
create trigger initialize_chat_user after insert on auth.users
for each row execute function public.initialize_chat_user();
insert into public.profiles(id) select id from auth.users on conflict do nothing;
insert into public.user_settings(user_id) select id from public.profiles on conflict do nothing;

create function public.record_audit(
  p_user_id uuid, p_event text, p_metadata jsonb default '{}',
  p_conversation_id uuid default null, p_run_id uuid default null
) returns jsonb language plpgsql security definer set search_path = '' as $$
begin
  if p_conversation_id is not null and not exists (
    select 1 from public.conversations where id = p_conversation_id and user_id = p_user_id
  ) then raise exception 'CONVERSATION_NOT_FOUND'; end if;
  if p_run_id is not null and not exists (
    select 1 from public.runs where id = p_run_id and user_id = p_user_id
  ) then raise exception 'RUN_NOT_FOUND'; end if;
  insert into public.audit_events(user_id,conversation_id,run_id,event,metadata)
    values (p_user_id,p_conversation_id,p_run_id,p_event,coalesce(p_metadata,'{}'));
  return jsonb_build_object('ok',true);
end;
$$;

create function public.create_conversation(p_user_id uuid, p_title text) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare v_conversation public.conversations;
begin
  insert into public.conversations(user_id,title) values (p_user_id,btrim(p_title)) returning * into v_conversation;
  perform public.record_audit(p_user_id,'conversation.created','{}',v_conversation.id);
  return to_jsonb(v_conversation) - 'user_id';
end;
$$;

create function public.rename_conversation(p_user_id uuid,p_conversation_id uuid,p_title text) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare v_conversation public.conversations;
begin
  update public.conversations set title=btrim(p_title),updated_at=now()
    where id=p_conversation_id and user_id=p_user_id returning * into v_conversation;
  if not found then raise exception 'CONVERSATION_NOT_FOUND'; end if;
  perform public.record_audit(p_user_id,'conversation.renamed','{}',p_conversation_id);
  return to_jsonb(v_conversation) - 'user_id';
end;
$$;

create function public.update_settings(p_user_id uuid,p_system_prompt text) returns jsonb
language plpgsql security definer set search_path = '' as $$
begin
  update public.user_settings set system_prompt=p_system_prompt where user_id=p_user_id;
  if not found then raise exception 'USER_NOT_FOUND'; end if;
  perform public.record_audit(p_user_id,'settings.updated');
  return jsonb_build_object('system_prompt',p_system_prompt);
end;
$$;

create function public.begin_run(
  p_user_id uuid,p_conversation_id uuid,p_request_id uuid,p_content text,p_model text,p_reserved_tokens bigint
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_account public.usage_accounts;
  v_run_id uuid;
  v_user_message_id uuid;
  v_assistant_message_id uuid;
  v_created timestamptz := clock_timestamp();
begin
  -- One user lock makes idempotency/rate limits atomic across conversations.
  perform 1 from public.profiles where id=p_user_id for update;
  if not found then raise exception 'USER_NOT_FOUND'; end if;
  perform 1 from public.conversations where id=p_conversation_id and user_id=p_user_id for update;
  if not found then raise exception 'CONVERSATION_NOT_FOUND'; end if;
  if exists (select 1 from public.runs where user_id=p_user_id and request_id=p_request_id) then
    raise exception 'REQUEST_ALREADY_PROCESSED';
  end if;
  if exists (select 1 from public.runs where conversation_id=p_conversation_id and status='running') then
    raise exception 'RUN_ALREADY_ACTIVE';
  end if;
  if (select count(*) from public.runs where user_id=p_user_id and created_at > now()-interval '1 minute') >= 10 then
    raise exception 'RUN_RATE_LIMITED';
  end if;
  if length(btrim(p_content)) not between 1 and 16000 or p_reserved_tokens is null or p_reserved_tokens <= 0
     or p_model is null or length(p_model) = 0 then raise exception 'INVALID_RUN'; end if;
  select * into strict v_account from public.usage_accounts where id='global' for update;
  if p_reserved_tokens > v_account.limit_tokens-v_account.used_tokens-v_account.reserved_tokens then
    raise exception 'BUDGET_EXCEEDED';
  end if;
  update public.usage_accounts set reserved_tokens=reserved_tokens+p_reserved_tokens where id='global';
  insert into public.runs(user_id,conversation_id,request_id,model,reserved_tokens)
    values (p_user_id,p_conversation_id,p_request_id,p_model,p_reserved_tokens) returning id into v_run_id;
  insert into public.messages(user_id,conversation_id,run_id,role,content,status,created_at)
    values (p_user_id,p_conversation_id,v_run_id,'user',btrim(p_content),'completed',v_created)
    returning id into v_user_message_id;
  insert into public.messages(user_id,conversation_id,run_id,role,status,created_at)
    values (p_user_id,p_conversation_id,v_run_id,'assistant','running',v_created+interval '1 microsecond')
    returning id into v_assistant_message_id;
  update public.conversations set updated_at=now() where id=p_conversation_id;
  perform public.record_audit(p_user_id,'run.started',jsonb_build_object('model',p_model,'reserved_tokens',p_reserved_tokens),p_conversation_id,v_run_id);
  perform public.record_audit(p_user_id,'message.created',jsonb_build_object('message_id',v_user_message_id),p_conversation_id,v_run_id);
  return jsonb_build_object('run_id',v_run_id,'user_message_id',v_user_message_id,'assistant_message_id',v_assistant_message_id);
end;
$$;

create function public.checkpoint_run(p_user_id uuid,p_run_id uuid,p_content text) returns jsonb
language plpgsql security definer set search_path = '' as $$
begin
  update public.runs set updated_at=now() where id=p_run_id and user_id=p_user_id and status='running';
  if not found then raise exception 'RUN_NOT_ACTIVE'; end if;
  update public.messages set content=p_content where run_id=p_run_id and user_id=p_user_id and role='assistant';
  return jsonb_build_object('ok',true);
end;
$$;

create function public.finish_run(
  p_user_id uuid,p_run_id uuid,p_status text,p_content text,
  p_input_tokens bigint,p_output_tokens bigint,p_error_code text
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_run public.runs; v_observed boolean; v_charged bigint;
begin
  if p_status is null or p_status not in ('completed','failed','interrupted')
     or p_input_tokens < 0 or p_output_tokens < 0
     or (p_input_tokens is null) <> (p_output_tokens is null) then
    raise exception 'INVALID_SETTLEMENT';
  end if;
  select * into v_run from public.runs where id=p_run_id and user_id=p_user_id for update;
  if not found then raise exception 'RUN_NOT_FOUND'; end if;
  if v_run.status <> 'running' then return jsonb_build_object('ok',true); end if;
  v_observed := p_input_tokens is not null and p_output_tokens is not null;
  v_charged := case when v_observed then p_input_tokens+p_output_tokens else v_run.reserved_tokens end;
  -- Unknown provider usage is charged conservatively, never refunded as if free.
  update public.usage_accounts set reserved_tokens=reserved_tokens-v_run.reserved_tokens,
    used_tokens=used_tokens+v_charged where id='global';
  insert into public.usage_ledger(user_id,conversation_id,run_id,charged_tokens,input_tokens,output_tokens,usage_observed)
    values (p_user_id,v_run.conversation_id,p_run_id,v_charged,p_input_tokens,p_output_tokens,v_observed);
  update public.runs set status=p_status,input_tokens=p_input_tokens,output_tokens=p_output_tokens,
    error_code=p_error_code,updated_at=now(),finished_at=now() where id=p_run_id;
  update public.messages set content=p_content,status=p_status where run_id=p_run_id and role='assistant';
  update public.conversations set updated_at=now() where id=v_run.conversation_id;
  perform public.record_audit(p_user_id,'run.'||p_status,jsonb_build_object('error_code',p_error_code),v_run.conversation_id,p_run_id);
  perform public.record_audit(p_user_id,'usage.recorded',jsonb_build_object('charged_tokens',v_charged,'usage_observed',v_observed),v_run.conversation_id,p_run_id);
  return jsonb_build_object('ok',true);
end;
$$;

create function public.recover_stale_runs(p_user_id uuid) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare v_run record; v_recovered integer := 0;
begin
  for v_run in select r.id,m.content from public.runs r join public.messages m on m.run_id=r.id and m.role='assistant'
    where r.user_id=p_user_id and r.status='running' and r.updated_at < now()-interval '10 minutes'
    for update of r skip locked
  loop
    perform public.finish_run(p_user_id,v_run.id,'interrupted',v_run.content,null,null,'worker_interrupted');
    v_recovered := v_recovered+1;
  end loop;
  return jsonb_build_object('recovered',v_recovered);
end;
$$;

-- Explicit grants override Supabase's default public-schema privileges.
alter table public.profiles enable row level security;
alter table public.user_settings enable row level security;
alter table public.conversations enable row level security;
alter table public.runs enable row level security;
alter table public.messages enable row level security;
alter table public.usage_accounts enable row level security;
alter table public.usage_ledger enable row level security;
alter table public.audit_events enable row level security;
create policy own_profile on public.profiles for select to authenticated using (id=(select auth.uid()));
create policy own_settings on public.user_settings for select to authenticated using (user_id=(select auth.uid()));
create policy own_conversations on public.conversations for select to authenticated using (user_id=(select auth.uid()));
create policy own_runs on public.runs for select to authenticated using (user_id=(select auth.uid()));
create policy own_messages on public.messages for select to authenticated using (user_id=(select auth.uid()));
create policy own_usage on public.usage_ledger for select to authenticated using (user_id=(select auth.uid()));
create policy own_audit on public.audit_events for select to authenticated using (user_id=(select auth.uid()));
revoke all on public.profiles,public.user_settings,public.conversations,public.runs,public.messages,
  public.usage_accounts,public.usage_ledger,public.audit_events from anon,authenticated,service_role;
grant select on public.profiles,public.user_settings,public.conversations,public.runs,public.messages,
  public.usage_ledger,public.audit_events to authenticated;
grant select on public.profiles,public.user_settings,public.conversations,public.runs,public.messages,
  public.usage_accounts,public.usage_ledger,public.audit_events to service_role;
revoke all on function public.initialize_chat_user() from public,anon,authenticated,service_role;
revoke all on function public.record_audit(uuid,text,jsonb,uuid,uuid),
  public.create_conversation(uuid,text),public.rename_conversation(uuid,uuid,text),public.update_settings(uuid,text),
  public.begin_run(uuid,uuid,uuid,text,text,bigint),public.checkpoint_run(uuid,uuid,text),
  public.finish_run(uuid,uuid,text,text,bigint,bigint,text),public.recover_stale_runs(uuid) from public,anon,authenticated;
grant execute on function public.record_audit(uuid,text,jsonb,uuid,uuid),
  public.create_conversation(uuid,text),public.rename_conversation(uuid,uuid,text),public.update_settings(uuid,text),
  public.begin_run(uuid,uuid,uuid,text,text,bigint),public.checkpoint_run(uuid,uuid,text),
  public.finish_run(uuid,uuid,text,text,bigint,bigint,text),public.recover_stale_runs(uuid) to service_role;

commit;
