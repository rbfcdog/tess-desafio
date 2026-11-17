-- Run as postgres against the migrated local/disposable database. No fixture survives.
begin;
do $$
declare
  owner_id uuid := gen_random_uuid();
  other_id uuid := gen_random_uuid();
  state_hash text;
  version_id uuid;
  lease_id uuid;
  claim jsonb;
  scopes jsonb := '["https://www.googleapis.com/auth/drive.readonly","https://www.googleapis.com/auth/gmail.readonly"]'::jsonb;
begin
  insert into auth.users(id,email) values
    (owner_id,owner_id::text||'@example.invalid'),(other_id,other_id::text||'@example.invalid');
  state_hash := replace(gen_random_uuid()::text,'-','')||replace(gen_random_uuid()::text,'-','');
  perform public.create_google_state(owner_id,state_hash,'opaque-encrypted-verifier');
  if (public.complete_google_oauth(owner_id,state_hash,'opaque-tokens',scopes,'owner@example.invalid')->>'ok')::boolean then
    raise exception 'OAuth completed without claiming its state';
  end if;
  claim := public.consume_google_state(state_hash);
  if claim->>'user_id' is distinct from owner_id::text then raise exception 'State owner was lost'; end if;
  if public.consume_google_state(state_hash)->>'user_id' is not null then raise exception 'OAuth state was replayed'; end if;
  if (public.complete_google_oauth(other_id,state_hash,'foreign-tokens',scopes,'other@example.invalid')->>'ok')::boolean then
    raise exception 'OAuth state crossed account boundaries';
  end if;
  begin
    perform public.complete_google_oauth(owner_id,state_hash,'opaque-tokens','[]'::jsonb,'owner@example.invalid');
    raise exception 'Missing readonly scopes were accepted';
  exception when raise_exception then
    if sqlerrm <> 'INVALID_SCOPES' then raise; end if;
  end;
  if not (public.complete_google_oauth(owner_id,state_hash,'initial-tokens',scopes,'owner@example.invalid')->>'ok')::boolean then
    raise exception 'Valid OAuth completion failed';
  end if;

  select version into version_id from public.google_connections where user_id=owner_id;
  claim := public.claim_google_refresh(owner_id,version_id);
  if not (claim->>'acquired')::boolean then raise exception 'Refresh lease was not acquired'; end if;
  lease_id := (claim->>'lease')::uuid;
  if (public.claim_google_refresh(owner_id,version_id)->>'acquired')::boolean then raise exception 'Two refreshes acquired the same version'; end if;
  if (public.save_google_refresh(other_id,lease_id,'foreign-tokens')->>'ok')::boolean then raise exception 'Refresh lease crossed account boundaries'; end if;
  update public.google_connections set refresh_until=now()-interval '1 second' where user_id=owner_id;
  if (public.save_google_refresh(owner_id,lease_id,'expired-tokens')->>'ok')::boolean then raise exception 'Expired refresh lease was saved'; end if;
  claim := public.claim_google_refresh(owner_id,version_id);
  if not (claim->>'acquired')::boolean then raise exception 'Expired refresh lease was not recoverable'; end if;
  lease_id := (claim->>'lease')::uuid;
  if not (public.save_google_refresh(owner_id,lease_id,'renewed-tokens')->>'ok')::boolean then raise exception 'Refresh did not persist'; end if;
  if (public.save_google_refresh(owner_id,lease_id,'replayed-tokens')->>'ok')::boolean then raise exception 'Refresh lease was replayed'; end if;
  if (public.claim_google_refresh(owner_id,version_id)->>'acquired')::boolean then raise exception 'Stale connection version acquired a lease'; end if;

  select version into version_id from public.google_connections where user_id=owner_id;
  claim := public.claim_google_refresh(owner_id,version_id);
  if not (claim->>'acquired')::boolean then raise exception 'In-flight refresh precondition failed'; end if;
  lease_id := (claim->>'lease')::uuid;
  state_hash := replace(gen_random_uuid()::text,'-','')||replace(gen_random_uuid()::text,'-','');
  perform public.create_google_state(owner_id,state_hash,'pending-verifier');
  perform public.consume_google_state(state_hash);
  perform public.disconnect_google(owner_id);
  if (public.save_google_refresh(owner_id,lease_id,'resurrected-tokens')->>'ok')::boolean then raise exception 'Refresh resurrected a disconnected account'; end if;
  if (public.complete_google_oauth(owner_id,state_hash,'resurrected-tokens',scopes,'owner@example.invalid')->>'ok')::boolean then raise exception 'Pending callback resurrected a disconnected account'; end if;
  if exists(select 1 from public.google_connections where user_id=owner_id) then raise exception 'Disconnect retained credentials'; end if;

  state_hash := replace(gen_random_uuid()::text,'-','')||replace(gen_random_uuid()::text,'-','');
  perform public.create_google_state(owner_id,state_hash,'replacement-verifier');
  perform public.consume_google_state(state_hash);
  perform public.complete_google_oauth(owner_id,state_hash,'replacement-tokens',scopes,'owner@example.invalid');
  if (public.save_google_refresh(owner_id,lease_id,'old-account-tokens')->>'ok')::boolean then raise exception 'Old refresh replaced a new connection'; end if;
  state_hash := replace(gen_random_uuid()::text,'-','')||replace(gen_random_uuid()::text,'-','');
  perform public.create_google_state(owner_id,state_hash,'expired-verifier');
  update public.google_oauth_states set expires_at=now()-interval '1 second' where user_id=owner_id;
  if public.consume_google_state(state_hash)->>'user_id' is not null then raise exception 'Expired OAuth state was accepted'; end if;
  if (public.complete_google_oauth(owner_id,state_hash,'expired-tokens',scopes,'owner@example.invalid')->>'ok')::boolean then raise exception 'Expired OAuth state replaced a connection'; end if;
  if (select tokens from public.google_connections where user_id=owner_id) is distinct from 'replacement-tokens' then raise exception 'Stale flow modified the current connection'; end if;

  if has_table_privilege('authenticated','public.google_connections','SELECT')
     or has_table_privilege('authenticated','public.google_oauth_states','SELECT')
     or has_column_privilege('authenticated','public.user_tools','secret','SELECT') then
    raise exception 'Browser role can read private integration credentials';
  end if;
  if has_function_privilege('authenticated','public.disconnect_google(uuid)','EXECUTE')
     or has_function_privilege('anon','public.consume_google_state(text)','EXECUTE') then
    raise exception 'Browser role can bypass authenticated connector routes';
  end if;
end;
$$;
rollback;
