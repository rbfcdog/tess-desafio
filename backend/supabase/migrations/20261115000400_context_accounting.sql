begin;

create table public.model_steps (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  conversation_id uuid not null,
  run_id uuid not null,
  kind text not null check (kind in ('reply','compaction')),
  reserved_tokens bigint not null check (reserved_tokens > 0),
  status text not null default 'running' check (status in ('running','completed','failed')),
  input_tokens bigint check (input_tokens >= 0),
  output_tokens bigint check (output_tokens >= 0),
  created_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  foreign key (run_id,user_id,conversation_id) references public.runs(id,user_id,conversation_id) on delete cascade
);
create index model_steps_run_order on public.model_steps(run_id,created_at,id);
create table public.context_summaries (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  conversation_id uuid not null,
  run_id uuid not null,
  through_message_id uuid references public.messages(id) on delete set null,
  scope text not null check (scope in ('history','loop')),
  summary text not null check (length(summary) between 1 and 64000),
  created_at timestamptz not null default clock_timestamp(),
  foreign key (run_id,user_id,conversation_id) references public.runs(id,user_id,conversation_id) on delete cascade
);
create index context_summaries_conversation on public.context_summaries(conversation_id,created_at desc,id desc);

create function public.reserve_model_step(
  p_user_id uuid,p_run_id uuid,p_kind text,p_input_tokens bigint,p_max_output_tokens bigint
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_run public.runs; v_account public.usage_accounts; v_amount bigint; v_step_id uuid;
begin
  if p_kind is null or p_kind not in ('reply','compaction') or p_input_tokens is null or p_input_tokens < 0
     or p_max_output_tokens is null or p_max_output_tokens not between 1 and 16384 then raise exception 'INVALID_STEP'; end if;
  select * into v_run from public.runs where id=p_run_id and user_id=p_user_id for update;
  if not found or v_run.status <> 'running' then raise exception 'RUN_NOT_ACTIVE'; end if;
  v_amount := p_input_tokens+p_max_output_tokens;
  select * into strict v_account from public.usage_accounts where id='global' for update;
  if v_amount > v_account.limit_tokens-v_account.used_tokens-v_account.reserved_tokens then raise exception 'BUDGET_EXCEEDED'; end if;
  update public.usage_accounts set reserved_tokens=reserved_tokens+v_amount where id='global';
  update public.runs set reserved_tokens=reserved_tokens+v_amount,updated_at=now() where id=p_run_id;
  insert into public.model_steps(user_id,conversation_id,run_id,kind,reserved_tokens)
    values (p_user_id,v_run.conversation_id,p_run_id,p_kind,v_amount) returning id into v_step_id;
  perform public.record_audit(p_user_id,'model.step.reserved',jsonb_build_object('step_id',v_step_id,'kind',p_kind,'reserved_tokens',v_amount),v_run.conversation_id,p_run_id);
  return jsonb_build_object('id',v_step_id,'reserved_tokens',v_amount);
end;
$$;

create function public.record_model_step(
  p_user_id uuid,p_step_id uuid,p_status text,p_input_tokens bigint,p_output_tokens bigint
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_step public.model_steps; v_run public.runs;
begin
  if p_status is null or p_status not in ('completed','failed') or p_input_tokens < 0 or p_output_tokens < 0
     or (p_input_tokens is null) <> (p_output_tokens is null)
     or (p_status='completed' and p_input_tokens is null) then raise exception 'INVALID_STEP'; end if;
  select * into v_step from public.model_steps where id=p_step_id and user_id=p_user_id;
  if not found then raise exception 'STEP_NOT_FOUND'; end if;
  -- Always lock the run before its steps, including cancellation/recovery paths.
  select * into v_run from public.runs where id=v_step.run_id and user_id=p_user_id for update;
  if v_run.status <> 'running' then return jsonb_build_object('ok',true); end if;
  update public.model_steps set status=p_status,input_tokens=p_input_tokens,output_tokens=p_output_tokens,finished_at=now()
    where id=p_step_id and status='running';
  if found then
    update public.runs set updated_at=now() where id=v_run.id;
    perform public.record_audit(p_user_id,'model.step.'||p_status,
      jsonb_build_object('step_id',p_step_id,'kind',v_step.kind,'input_tokens',p_input_tokens,'output_tokens',p_output_tokens),v_run.conversation_id,v_run.id);
  end if;
  return jsonb_build_object('ok',true);
end;
$$;

create function public.save_summary(
  p_user_id uuid,p_conversation_id uuid,p_run_id uuid,p_through_message_id uuid,p_summary text,p_scope text default 'history'
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_summary public.context_summaries; v_covered_at timestamptz;
begin
  perform 1 from public.runs where id=p_run_id and user_id=p_user_id and conversation_id=p_conversation_id and status='running' for update;
  if not found then raise exception 'RUN_NOT_ACTIVE'; end if;
  if p_scope='history' then
    select created_at into v_covered_at from public.messages where id=p_through_message_id and user_id=p_user_id
      and conversation_id=p_conversation_id and role='assistant' and status='completed';
    if not found then raise exception 'INVALID_SUMMARY_COVERAGE'; end if;
    if exists (select 1 from public.context_summaries s join public.messages m on m.id=s.through_message_id
      where s.conversation_id=p_conversation_id and s.scope='history' and m.created_at > v_covered_at) then
      raise exception 'SUMMARY_OUTDATED';
    end if;
  elsif p_scope is distinct from 'loop' then raise exception 'INVALID_SUMMARY_SCOPE'; end if;
  insert into public.context_summaries(user_id,conversation_id,run_id,through_message_id,scope,summary)
    values (p_user_id,p_conversation_id,p_run_id,p_through_message_id,p_scope,p_summary) returning * into v_summary;
  perform public.record_audit(p_user_id,'context.compacted',jsonb_build_object('summary_id',v_summary.id,'scope',p_scope,'through_message_id',p_through_message_id),p_conversation_id,p_run_id);
  return jsonb_build_object('id',v_summary.id,'through_message_id',p_through_message_id,'summary',p_summary,'scope',p_scope,'created_at',v_summary.created_at);
end;
$$;

-- Clean signature replacement: callers without files use the optional empty array.
drop function public.begin_run(uuid,uuid,uuid,text,text,bigint);
create function public.begin_run(
  p_user_id uuid,p_conversation_id uuid,p_request_id uuid,p_content text,p_model text,p_reserved_tokens bigint,p_attachment_ids uuid[] default '{}'
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_account public.usage_accounts; v_run_id uuid; v_user_message_id uuid; v_assistant_message_id uuid; v_created timestamptz := clock_timestamp();
begin
  perform 1 from public.profiles where id=p_user_id for update;
  if not found then raise exception 'USER_NOT_FOUND'; end if;
  perform 1 from public.conversations where id=p_conversation_id and user_id=p_user_id for update;
  if not found then raise exception 'CONVERSATION_NOT_FOUND'; end if;
  if exists (select 1 from public.runs where user_id=p_user_id and request_id=p_request_id) then raise exception 'REQUEST_ALREADY_PROCESSED'; end if;
  if exists (select 1 from public.runs where conversation_id=p_conversation_id and status='running') then raise exception 'RUN_ALREADY_ACTIVE'; end if;
  if (select count(*) from public.runs where user_id=p_user_id and created_at > now()-interval '1 minute') >= 10 then raise exception 'RUN_RATE_LIMITED'; end if;
  if p_content is null or length(btrim(p_content)) > 16000 or cardinality(coalesce(p_attachment_ids,'{}')) > 5
     or (length(btrim(p_content))=0 and cardinality(coalesce(p_attachment_ids,'{}'))=0)
     or p_reserved_tokens is null or p_reserved_tokens <= 0 or p_model is null or length(p_model)=0 then raise exception 'INVALID_RUN'; end if;
  select * into strict v_account from public.usage_accounts where id='global' for update;
  if p_reserved_tokens > v_account.limit_tokens-v_account.used_tokens-v_account.reserved_tokens then raise exception 'BUDGET_EXCEEDED'; end if;
  update public.usage_accounts set reserved_tokens=reserved_tokens+p_reserved_tokens where id='global';
  insert into public.runs(user_id,conversation_id,request_id,model,reserved_tokens)
    values (p_user_id,p_conversation_id,p_request_id,p_model,p_reserved_tokens) returning id into v_run_id;
  insert into public.messages(user_id,conversation_id,run_id,role,content,status,created_at)
    values (p_user_id,p_conversation_id,v_run_id,'user',btrim(p_content),'completed',v_created) returning id into v_user_message_id;
  insert into public.messages(user_id,conversation_id,run_id,role,status,created_at)
    values (p_user_id,p_conversation_id,v_run_id,'assistant','running',v_created+interval '1 microsecond') returning id into v_assistant_message_id;
  perform public.bind_run_attachments(p_user_id,v_run_id,coalesce(p_attachment_ids,'{}'));
  update public.conversations set updated_at=now() where id=p_conversation_id;
  perform public.record_audit(p_user_id,'run.started',jsonb_build_object('model',p_model,'reserved_tokens',p_reserved_tokens),p_conversation_id,v_run_id);
  perform public.record_audit(p_user_id,'message.created',jsonb_build_object('message_id',v_user_message_id),p_conversation_id,v_run_id);
  return jsonb_build_object('run_id',v_run_id,'user_message_id',v_user_message_id,'assistant_message_id',v_assistant_message_id);
end;
$$;

create or replace function public.finish_run(
  p_user_id uuid,p_run_id uuid,p_status text,p_content text,p_input_tokens bigint,p_output_tokens bigint,p_error_code text
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_run public.runs; v_observed boolean; v_charged bigint; v_count integer; v_known boolean; v_input bigint; v_output bigint;
begin
  if p_status is null or p_status not in ('completed','failed','interrupted') or p_input_tokens < 0 or p_output_tokens < 0
     or (p_input_tokens is null) <> (p_output_tokens is null) then raise exception 'INVALID_SETTLEMENT'; end if;
  select * into v_run from public.runs where id=p_run_id and user_id=p_user_id for update;
  if not found then raise exception 'RUN_NOT_FOUND'; end if;
  if v_run.status <> 'running' then return jsonb_build_object('ok',true); end if;
  if p_status='completed' and exists (select 1 from public.model_steps where run_id=p_run_id and status='running') then raise exception 'INVALID_SETTLEMENT'; end if;
  update public.model_steps set status='failed',finished_at=now() where run_id=p_run_id and status='running';
  select count(*),bool_and(input_tokens is not null and output_tokens is not null),sum(input_tokens),sum(output_tokens)
    into v_count,v_known,v_input,v_output from public.model_steps where run_id=p_run_id;
  if v_count > 0 then
    if v_known then
      if p_input_tokens is not null and (p_input_tokens <> v_input or p_output_tokens <> v_output) then raise exception 'USAGE_MISMATCH'; end if;
      p_input_tokens := v_input;
      p_output_tokens := v_output;
    elsif p_input_tokens is not null then raise exception 'USAGE_MISMATCH'; end if;
  end if;
  v_observed := p_input_tokens is not null and p_output_tokens is not null;
  v_charged := case when v_observed then p_input_tokens+p_output_tokens else v_run.reserved_tokens end;
  update public.usage_accounts set reserved_tokens=reserved_tokens-v_run.reserved_tokens,used_tokens=used_tokens+v_charged where id='global';
  insert into public.usage_ledger(user_id,conversation_id,run_id,charged_tokens,input_tokens,output_tokens,usage_observed)
    values (p_user_id,v_run.conversation_id,p_run_id,v_charged,p_input_tokens,p_output_tokens,v_observed);
  update public.runs set status=p_status,input_tokens=p_input_tokens,output_tokens=p_output_tokens,error_code=p_error_code,updated_at=now(),finished_at=now() where id=p_run_id;
  update public.messages set content=p_content,status=p_status where run_id=p_run_id and role='assistant';
  update public.conversations set updated_at=now() where id=v_run.conversation_id;
  perform public.record_audit(p_user_id,'run.'||p_status,jsonb_build_object('error_code',p_error_code),v_run.conversation_id,p_run_id);
  perform public.record_audit(p_user_id,'usage.recorded',jsonb_build_object('charged_tokens',v_charged,'usage_observed',v_observed),v_run.conversation_id,p_run_id);
  return jsonb_build_object('ok',true);
end;
$$;

alter table public.model_steps enable row level security;
alter table public.context_summaries enable row level security;
create policy own_model_steps on public.model_steps for select to authenticated using (user_id=(select auth.uid()));
create policy own_context_summaries on public.context_summaries for select to authenticated using (user_id=(select auth.uid()));
revoke all on public.model_steps,public.context_summaries from public,anon,authenticated,service_role;
grant select on public.model_steps,public.context_summaries to authenticated,service_role;
revoke all on function public.reserve_model_step(uuid,uuid,text,bigint,bigint),public.record_model_step(uuid,uuid,text,bigint,bigint),
  public.save_summary(uuid,uuid,uuid,uuid,text,text),public.begin_run(uuid,uuid,uuid,text,text,bigint,uuid[]) from public,anon,authenticated;
grant execute on function public.reserve_model_step(uuid,uuid,text,bigint,bigint),public.record_model_step(uuid,uuid,text,bigint,bigint),
  public.save_summary(uuid,uuid,uuid,uuid,text,text),public.begin_run(uuid,uuid,uuid,text,text,bigint,uuid[]) to service_role;
notify pgrst,'reload schema';
commit;
