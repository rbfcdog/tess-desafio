-- Teto global de tokens: 1M esgotou durante a verificação; 5M cobre a semana de avaliação.
update public.usage_accounts set limit_tokens = 5000000 where id = 'global';
