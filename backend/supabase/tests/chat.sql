-- Run against a disposable/local database as postgres. All test data is rolled back.
begin;
do $$
declare
  u1 uuid := gen_random_uuid();
  u2 uuid := gen_random_uuid();
  c1 uuid;
  c2 uuid;
  request_id uuid := gen_random_uuid();
  started jsonb;
  v_run_id uuid;
  v_step_id uuid;
  v_step2_id uuid;
  v_covered_message uuid;
  v_foreign_message uuid;
  account public.usage_accounts;
  n integer;
begin
  insert into auth.users(id,email) values (u1,u1::text||'@example.invalid'),(u2,u2::text||'@example.invalid');
  c1 := (public.create_conversation(u1,'First owner')->>'id')::uuid;
  c2 := (public.create_conversation(u2,'Second owner')->>'id')::uuid;


  begin
    perform public.begin_run(u1,c2,gen_random_uuid(),'Foreign','model',100);
    raise exception 'Foreign owner was allowed';
  exception when raise_exception then
    if sqlerrm <> 'CONVERSATION_NOT_FOUND' then raise; end if;
  end;

  update public.usage_accounts set limit_tokens=100,used_tokens=0,reserved_tokens=0 where id='global';
  started := public.begin_run(u1,c1,request_id,'Hello','model',50);
  v_run_id := (started->>'run_id')::uuid;
  begin
    perform public.begin_run(u1,c1,request_id,'Duplicate','model',50);
    raise exception 'Duplicate request was allowed';
  exception when raise_exception then
    if sqlerrm <> 'REQUEST_ALREADY_PROCESSED' then raise; end if;
  end;
  begin
    perform public.begin_run(u1,c1,gen_random_uuid(),'Concurrent','model',10);
    raise exception 'Second active run was allowed';
  exception when raise_exception then
    if sqlerrm <> 'RUN_ALREADY_ACTIVE' then raise; end if;
  end;
  perform public.checkpoint_run(u1,v_run_id,'Partial answer');
  perform public.finish_run(u1,v_run_id,'completed','Answer',10,20,null);
  perform public.finish_run(u1,v_run_id,'completed','Must not replace',100,100,null);
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 30 or account.reserved_tokens <> 0 then raise exception 'Settlement was not idempotent'; end if;
  select count(*) into n from public.messages where conversation_id=c1;
  if n <> 2 then raise exception 'Duplicate messages were inserted'; end if;
  if not exists (select 1 from public.messages where run_id=v_run_id and role='assistant' and content='Answer') then
    raise exception 'Final content was not persisted';
  end if;

  begin
    perform public.begin_run(u1,c1,gen_random_uuid(),'Too expensive','model',71);
    raise exception 'Budget cap was bypassed';
  exception when raise_exception then
    if sqlerrm <> 'BUDGET_EXCEEDED' then raise; end if;
  end;
  select count(*) into n from public.messages where conversation_id=c1;
  if n <> 2 then raise exception 'Rejected run left messages behind'; end if;

  started := public.begin_run(u1,c1,gen_random_uuid(),'Unknown usage','model',50);
  v_run_id := (started->>'run_id')::uuid;
  perform public.finish_run(u1,v_run_id,'failed','Partial',null,null,'provider_failed');
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 80 or account.reserved_tokens <> 0 then raise exception 'Unknown usage was refunded'; end if;

  started := public.begin_run(u1,c1,gen_random_uuid(),'Interrupted','model',20);
  v_run_id := (started->>'run_id')::uuid;
  update public.runs set updated_at=now()-interval '11 minutes' where id=v_run_id;
  perform public.recover_stale_runs(u1);
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 100 or account.reserved_tokens <> 0 then raise exception 'Stale reservation was not settled'; end if;
  if not exists (select 1 from public.runs where id=v_run_id and status='interrupted') then raise exception 'Stale run did not transition'; end if;
  if not exists (select 1 from public.audit_events where user_id=u1 and event='usage.recorded') then raise exception 'Usage audit is missing'; end if;

  -- Summary and reply calls share one run/account; actual multi-step usage is not double charged.
  update public.usage_accounts set limit_tokens=200 where id='global';
  started := public.begin_run(u2,c2,gen_random_uuid(),'Multiple model calls','model',1);
  v_run_id := (started->>'run_id')::uuid;
  v_step_id := (public.reserve_model_step(u2,v_run_id,'compaction',20,30)->>'id')::uuid;
  perform public.record_model_step(u2,v_step_id,'completed',20,10);
  perform public.record_model_step(u2,v_step_id,'completed',999,999);
  v_step2_id := (public.reserve_model_step(u2,v_run_id,'reply',10,20)->>'id')::uuid;
  perform public.record_model_step(u2,v_step2_id,'completed',10,5);
  begin
    perform public.finish_run(u2,v_run_id,'completed','Wrong totals',1,1,null);
    raise exception 'Mismatched usage was accepted';
  exception when raise_exception then
    if sqlerrm <> 'USAGE_MISMATCH' then raise; end if;
  end;
  perform public.finish_run(u2,v_run_id,'completed','Counted summary and reply',30,15,null);
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 145 or account.reserved_tokens <> 0 then raise exception 'Multi-step usage was miscounted'; end if;

  started := public.begin_run(u2,c2,gen_random_uuid(),'Unknown model step','model',1);
  v_run_id := (started->>'run_id')::uuid;
  v_step_id := (public.reserve_model_step(u2,v_run_id,'reply',10,10)->>'id')::uuid;
  update public.runs set updated_at=now()-interval '11 minutes' where id=v_run_id;
  perform public.recover_stale_runs(u2);
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 166 or account.reserved_tokens <> 0 then raise exception 'Unknown step was refunded'; end if;
  if not exists (select 1 from public.model_steps where id=v_step_id and status='failed') then raise exception 'Orphan model step remained active'; end if;

  started := public.begin_run(u2,c2,gen_random_uuid(),'Cannot reserve expensive step','model',1);
  v_run_id := (started->>'run_id')::uuid;
  begin
    perform public.reserve_model_step(u2,v_run_id,'reply',20,20);
    raise exception 'Model step bypassed global budget';
  exception when raise_exception then
    if sqlerrm <> 'BUDGET_EXCEEDED' then raise; end if;
  end;
  if exists (select 1 from public.model_steps where run_id=v_run_id) then raise exception 'Rejected step left billing state'; end if;
  perform public.finish_run(u2,v_run_id,'failed','',0,0,'no_provider_call');

  started := public.begin_run(u1,c1,gen_random_uuid(),'Persist summary coverage','model',1);
  v_run_id := (started->>'run_id')::uuid;
  select id into v_covered_message from public.messages where conversation_id=c1 and role='assistant' and status='completed' limit 1;
  select id into v_foreign_message from public.messages where conversation_id=c2 and role='assistant' and status='completed' limit 1;
  begin
    perform public.save_summary(u1,c1,v_run_id,v_foreign_message,'Must not cover foreign history','history');
    raise exception 'Foreign summary coverage accepted';
  exception when raise_exception then
    if sqlerrm <> 'INVALID_SUMMARY_COVERAGE' then raise; end if;
  end;
  perform public.save_summary(u1,c1,v_run_id,v_covered_message,'Preserved prior facts','history');
  perform public.finish_run(u1,v_run_id,'failed','',0,0,'verification_no_provider');
  if not exists (select 1 from public.context_summaries where run_id=v_run_id and through_message_id=v_covered_message) then
    raise exception 'Summary was not persisted with exact coverage';
  end if;

  -- Known usage frees headroom immediately; an unknown reply retains only its own estimate.
  update public.usage_accounts set limit_tokens=100,used_tokens=0,reserved_tokens=0 where id='global';
  v_run_id := (public.begin_run(u2,c2,gen_random_uuid(),'Tight multi-step budget','model',1)->>'run_id')::uuid;
  v_step_id := (public.reserve_model_step(u2,v_run_id,'compaction',20,30)->>'id')::uuid;
  perform public.record_model_step(u2,v_step_id,'completed',20,10);
  perform public.record_model_step(u2,v_step_id,'completed',999,999);
  select * into account from public.usage_accounts where id='global';
  if account.reserved_tokens <> 31 or (select reserved_tokens from public.runs where id=v_run_id) <> 31 then
    raise exception 'Known usage did not reconcile exactly once';
  end if;
  v_step2_id := (public.reserve_model_step(u2,v_run_id,'reply',20,30)->>'id')::uuid;
  perform public.record_model_step(u2,v_step2_id,'failed',null,null);
  select * into account from public.usage_accounts where id='global';
  if account.reserved_tokens <> 81 then raise exception 'Unknown reply lost its reservation'; end if;
  perform public.finish_run(u2,v_run_id,'failed','',null,null,'provider_failed');
  perform public.finish_run(u2,v_run_id,'failed','',null,null,'provider_failed');
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 81 or account.reserved_tokens <> 0 then raise exception 'Mixed usage was miscounted'; end if;
  if not exists (select 1 from public.usage_ledger where run_id=v_run_id and charged_tokens=81 and not usage_observed) then
    raise exception 'Mixed usage ledger was incorrect';
  end if;

  -- Provider-reported usage above the estimate must also remain accounted for.
  v_run_id := (public.begin_run(u2,c2,gen_random_uuid(),'Usage above estimate','model',1)->>'run_id')::uuid;
  v_step_id := (public.reserve_model_step(u2,v_run_id,'reply',5,5)->>'id')::uuid;
  perform public.record_model_step(u2,v_step_id,'failed',15,5);
  select * into account from public.usage_accounts where id='global';
  if account.reserved_tokens <> 21 or (select reserved_tokens from public.runs where id=v_run_id) <> 21 then
    raise exception 'Usage above the estimate was undercounted';
  end if;
  perform public.finish_run(u2,v_run_id,'failed','',15,5,'provider_failed');
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 101 or account.reserved_tokens <> 0 then raise exception 'Reported usage was not settled'; end if;
  perform set_config('test.chat_owner',u1::text,true);
  perform set_config('test.chat_foreign_conversation',c2::text,true);
end;
$$;

do $$
declare
  u uuid := gen_random_uuid(); other_user uuid := gen_random_uuid();
  c uuid; r uuid; s uuid; oldest uuid; snapshot jsonb; refreshed jsonb;
  account public.usage_accounts; total integer; distinct_ids integer;
begin
  insert into auth.users(id,email) values (u,u::text||'@example.invalid'),(other_user,other_user::text||'@example.invalid');
  if public.list_conversations(other_user) <> '{"conversations":[]}'::jsonb then raise exception 'Empty listing was incorrect'; end if;
  c := (public.create_conversation(u,'Acknowledgement recovery')->>'id')::uuid;
  perform public.create_conversation(other_user,'Private');
  update public.usage_accounts set limit_tokens=1000,used_tokens=0,reserved_tokens=0 where id='global';
  r := (public.begin_run(u,c,gen_random_uuid(),'Summary then lost reservation acknowledgement','model',1)->>'run_id')::uuid;
  s := (public.reserve_model_step(u,r,'compaction',20,30)->>'id')::uuid;
  perform public.record_model_step(u,s,'completed',20,10);
  s := (public.reserve_model_step(u,r,'reply',20,30)->>'id')::uuid;
  begin
    perform public.finish_unattempted_run(other_user,r,'failed','',null);
    raise exception 'Foreign owner settled a run';
  exception when raise_exception then
    if sqlerrm <> 'RUN_NOT_FOUND' then raise; end if;
  end;
  perform public.finish_unattempted_run(u,r,'failed','Preserved partial','lost_acknowledgement');
  perform public.finish_unattempted_run(u,r,'failed','Must not replace','lost_acknowledgement');
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 30 or account.reserved_tokens <> 0 then raise exception 'Unattempted step was charged or known usage lost'; end if;
  if not exists (select 1 from public.messages where run_id=r and role='assistant' and status='failed' and content='Preserved partial') then
    raise exception 'Acknowledgement recovery lost the response state';
  end if;
  if not exists (select 1 from public.model_steps where id=s and status='failed' and input_tokens=0 and output_tokens=0) then
    raise exception 'Unattempted step was not reconciled';
  end if;
  if (select count(*) from public.usage_ledger where run_id=r) <> 1 then raise exception 'Cleanup was not idempotent'; end if;

  -- A new run is immediately allowed; cleanup also wins against a late reservation.
  r := (public.begin_run(u,c,gen_random_uuid(),'No reservation committed','model',1)->>'run_id')::uuid;
  perform public.finish_unattempted_run(u,r,'interrupted','',null);
  begin
    perform public.reserve_model_step(u,r,'reply',10,10);
    raise exception 'Late reservation reopened a settled run';
  exception when raise_exception then
    if sqlerrm <> 'RUN_NOT_ACTIVE' then raise; end if;
  end;
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 30 or account.reserved_tokens <> 0 then raise exception 'Uncommitted reservation was charged'; end if;

  -- Previously attempted unknown usage is not refunded by pre-provider cleanup.
  r := (public.begin_run(u,c,gen_random_uuid(),'Retain previous unknown usage','model',1)->>'run_id')::uuid;
  s := (public.reserve_model_step(u,r,'reply',10,10)->>'id')::uuid;
  perform public.record_model_step(u,s,'failed',null,null);
  perform public.reserve_model_step(u,r,'reply',10,10);
  perform public.finish_unattempted_run(u,r,'failed','',null);
  select * into account from public.usage_accounts where id='global';
  if account.used_tokens <> 51 or account.reserved_tokens <> 0 then raise exception 'Cleanup refunded attempted unknown usage'; end if;

  -- The list is one owner-scoped snapshot, including rows beyond the REST page cap.
  update public.conversations set updated_at=now()-interval '1 second' where id=c;
  insert into public.conversations(user_id,title,updated_at)
    select u,'Paging '||n,now()-(n+1)*interval '1 second' from generate_series(1,1000) n;
  snapshot := public.list_conversations(u);
  select count(*),count(distinct item->>'id') into total,distinct_ids from jsonb_array_elements(snapshot->'conversations') item;
  if total <> 1001 or distinct_ids <> 1001 then raise exception 'Listing omitted or duplicated conversations'; end if;
  if exists (select 1 from jsonb_array_elements(snapshot->'conversations') item
    where not exists (select 1 from public.conversations where id=(item->>'id')::uuid and user_id=u)) then
    raise exception 'Listing exposed another owner';
  end if;
  oldest := (snapshot->'conversations'->1000->>'id')::uuid;
  perform public.rename_conversation(u,oldest,'Moved to newest');
  refreshed := public.list_conversations(u);
  if refreshed->'conversations'->0->>'id' <> oldest::text or refreshed->'conversations'->0->>'title' <> 'Moved to newest' then
    raise exception 'Listing did not preserve newest-first ordering';
  end if;
end;
$$;

-- Change roles between statements, never from inside a PL/pgSQL execution frame.
set local role authenticated;
select set_config('request.jwt.claim.sub',current_setting('test.chat_owner'),true);
do $$
begin
  if exists (select 1 from public.conversations where id=current_setting('test.chat_foreign_conversation')::uuid) then
    raise exception 'RLS exposed a foreign conversation';
  end if;
  if has_function_privilege('authenticated','public.create_conversation(uuid,text)','EXECUTE') then
    raise exception 'Browser can invoke trusted mutation';
  end if;
  if has_function_privilege('authenticated','public.list_conversations(uuid)','EXECUTE')
    or has_function_privilege('authenticated','public.finish_unattempted_run(uuid,uuid,text,text,text)','EXECUTE') then
    raise exception 'Browser can invoke trusted snapshot or recovery';
  end if;
  if has_table_privilege('authenticated','public.conversations','INSERT') then
    raise exception 'Browser can bypass trusted write path';
  end if;
end;
$$;
reset role;
rollback;
