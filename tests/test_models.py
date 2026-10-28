"""Model factory: LangChain init_chat_model, as in the LangChain Academy notebooks."""

from a6g_common.config import ModelRole
from a6g_common.models import chat_model, fake_model, one_sentence, structured_invoke
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel


def test_default_roles_use_gpt_5_nano_like_the_notebooks(cfg) -> None:
    model = chat_model(ModelRole.COORDINATOR_LLM, cfg.models, env={"OPENAI_API_KEY": "sk-test"})
    assert isinstance(model, ChatOpenAI)  # provider inferred from "gpt-5-nano"
    assert model.model_name == "gpt-5-nano"
    assert model.openai_api_base is None  # the OpenAI API, not a self-hosted server
    assert model.temperature is None  # GPT-5 models only accept their default
    assert model.openai_api_key.get_secret_value() == "sk-test"


def test_base_url_selects_a_self_hosted_openai_compatible_server(cfg) -> None:
    endpoint = cfg.models.roles[ModelRole.ONBOARD_VLM]
    endpoint.base_url, endpoint.model = "http://192.168.123.18:8082/v1", "qwen2.5-vl-3b"
    model = chat_model(ModelRole.ONBOARD_VLM, cfg.models, env={})
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == "http://192.168.123.18:8082/v1"
    assert model.model_name == "qwen2.5-vl-3b"


def test_env_overrides_in_models_yaml(config_dir) -> None:
    from a6g_common.config import load_config

    cfg = load_config(config_dir, env={"A6G_ONBOARD_VLM_URL": "http://dock:8082/v1"})
    assert cfg.models.roles[ModelRole.ONBOARD_VLM].base_url == "http://dock:8082/v1"
    assert load_config(config_dir, env={}).models.roles[ModelRole.ONBOARD_VLM].base_url is None


class Verdict(BaseModel):
    ok: bool
    label: str


async def test_structured_invoke_json_path_retry_and_fallback() -> None:
    msgs = [HumanMessage("judge")]
    good = fake_model(['```json\n{"ok": true, "label": "bag"}\n```'])
    value, fell_back = await structured_invoke(
        good, Verdict, msgs, lambda: Verdict(ok=False, label="x")
    )
    assert value == Verdict(ok=True, label="bag") and not fell_back

    retry = fake_model(["not json", '{"ok": false, "label": "cat"}'])
    value, fell_back = await structured_invoke(
        retry, Verdict, msgs, lambda: Verdict(ok=False, label="x")
    )
    assert value.label == "cat" and not fell_back

    bad = fake_model(["nope", "still nope"])
    value, fell_back = await structured_invoke(
        bad, Verdict, msgs, lambda: Verdict(ok=False, label="fb")
    )
    assert value.label == "fb" and fell_back


async def test_one_sentence_falls_back_without_model() -> None:
    assert await one_sentence(None, "s", "p", "template") == ("template", True)
