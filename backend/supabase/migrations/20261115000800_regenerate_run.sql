begin;

-- Replays the conversation from its last completed user message; the previous replies
-- are removed from the transcript while their runs remain as billing history.
create function public.begin_regenerate_run(p_user_id uuid,p_conversation_id uuid,p_request_id uuid,p_model text,p_reserved_tokens bigint)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_account public.usage_accounts; v_run_id uuid; v_user_message_id uuid; v_assistant_message_id uuid; v_created timestamptz := clock_timestamp();
begin
  perform 1 from public.profiles where id=p_user_id for update;
  if not found then raise exception 'USER_NOT_FOUND'; end if;
  perform 1 from public.conversations where id=p_conversation_id and user_id=p_user_id for update;
  if not found then raise exception 'CONVERSATION_NOT_FOUND'; end if;
  if exists (select 1 from public.runs where user_id=p_user_id and request_id=p_request_id) then raise exception 'REQUEST_ALREADY_PROCESSED'; end if;
  if exists (select 1 from public.runs where conversation_id=p_conversation_id and status='running') then raise exception 'RUN_ALREADY_ACTIVE'; end if;
  if (select count(*) from public.runs where user_id=p_user_id and created_at > now()-interval '1 minute') >= 10 then raise exception 'RUN_RATE_LIMITED'; end if;
  if p_reserved_tokens is null or p_reserved_tokens <= 0 or p_model is null or length(p_model)=0 then raise exception 'INVALID_RUN'; end if;
  select id into v_user_message_id from public.messages
    where conversation_id=p_conversation_id and user_id=p_user_id and role='user' and status='completed'
    order by created_at desc,id desc limit 1;
  if not found then raise exception 'INVALID_RUN'; end if;
  delete from public.messages where conversation_id=p_conversation_id and user_id=p_user_id
    and role='assistant' and status in ('completed','failed','interrupted')
    and (created_at,id) > (select created_at,id from public.messages where id=v_user_message_id);
  select * into strict v_account from public.usage_accounts where id='global' for update;
  if p_reserved_tokens > v_account.limit_tokens-v_account.used_tokens-v_account.reserved_tokens then raise exception 'BUDGET_EXCEEDED'; end if;
  update public.usage_accounts set reserved_tokens=reserved_tokens+p_reserved_tokens where id='global';
  insert into public.runs(user_id,conversation_id,request_id,model,reserved_tokens)
    values (p_user_id,p_conversation_id,p_request_id,p_model,p_reserved_tokens) returning id into v_run_id;
  insert into public.messages(user_id,conversation_id,run_id,role,status,created_at)
    values (p_user_id,p_conversation_id,v_run_id,'assistant','running',v_created+interval '1 microsecond') returning id into v_assistant_message_id;
  update public.conversations set updated_at=now() where id=p_conversation_id;
  perform public.record_audit(p_user_id,'run.regenerated',jsonb_build_object('model',p_model,'reserved_tokens',p_reserved_tokens,'user_message_id',v_user_message_id),p_conversation_id,v_run_id);
  return jsonb_build_object('run_id',v_run_id,'user_message_id',v_user_message_id,'assistant_message_id',v_assistant_message_id);
end;
$$;

revoke all on function public.begin_regenerate_run(uuid,uuid,uuid,text,bigint) from public,anon,authenticated;
grant execute on function public.begin_regenerate_run(uuid,uuid,uuid,text,bigint) to service_role;
notify pgrst,'reload schema';
commit;
