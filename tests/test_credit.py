from seele import config, credit


def test_meter_counts_growth_of_a_process_total():
    m = credit.Meter()
    assert m.add(0.004) == 0.004
    assert round(m.add(0.005), 6) == 0.001  # live input: the next turn's total includes the first
    assert m.add(0.002) == 0.002  # lower = a new process


def test_money_summary_and_usage():
    assert credit.money(12.3) == "$12.30"
    assert credit.money(0.004) == "$0.004"
    assert credit.summary(200, 12.3) == "$12.30 of $200.00 API credit"
    assert credit.summary(0, 1) == "$1.00 API spend"
    text = credit.usage_text(200, 50, 3)
    assert "**$50.00** of $200.00 used · $150.00 left" in text and "3 runs" in text


def test_crossed_and_run_cap():
    assert credit.crossed(200, 90, 101) == 0.5
    assert credit.crossed(200, 101, 102) is None
    assert credit.crossed(200, 150, 185) == 0.9
    assert credit.crossed(0, 0, 10) is None
    assert credit.run_cap(200, 190, 40) == 10
    assert credit.run_cap(200, 0, 40) == 40
    assert credit.run_cap(0, 0, 0) is None
    assert credit.OUT_OF_CREDIT_RE.search("Credit balance is too low to access the Anthropic API")


def test_config_reads_api_billing(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('token = "x"\n[accounts.claude]\nconfig_dir = "~/.claude"\n'
                 '[accounts.claude-api]\nconfig_dir = "~/.claude-api"\nbilling = "api"\nbudget = 200\n'
                 'max_run_usd = 40\nspent = 1.5\n')
    cfg = config.load(p)
    plan, api = cfg.accounts
    assert not plan.api and api.api
    assert (api.budget, api.max_run_usd, api.spent) == (200, 40, 1.5)


def test_state_spend_round_trip(tmp_path):
    s = config.State(tmp_path / "s.json")
    s.add_spend("claude-api", 0.25, run=True)
    s.add_spend("claude-api", 0.25)
    s.save()
    again = config.State(tmp_path / "s.json")
    assert again.spent("claude-api") == 0.5 and again.spend["claude-api"]["runs"] == 1
