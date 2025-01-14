from __future__ import annotations

import time
from typing import List, Optional

import openai
from colorama import Fore, Style
from openai.error import APIError, RateLimitError, Timeout

from autogpt.api_manager import api_manager
from autogpt.config import Config
from autogpt.logs import logger
from autogpt.types.openai import Message

from transformers import AutoTokenizer, AutoModelForCausalLM



CFG = Config()

openai.api_key = CFG.openai_api_key


def call_ai_function(
    function: str, args: list, description: str, model: str | None = None
) -> str:
    """Call an AI function

    This is a magic function that can do anything with no-code. See
    https://github.com/Torantulino/AI-Functions for more info.

    Args:
        function (str): The function to call
        args (list): The arguments to pass to the function
        description (str): The description of the function
        model (str, optional): The model to use. Defaults to None.

    Returns:
        str: The response from the function
    """
    if model is None:
        model = CFG.smart_llm_model
    # For each arg, if any are None, convert to "None":
    args = [str(arg) if arg is not None else "None" for arg in args]
    # parse args to comma separated string
    args: str = ", ".join(args)
    messages: List[Message] = [
        {
            "role": "system",
            "content": f"You are now the following python function: ```# {description}"
            f"\n{function}```\n\nOnly respond with your `return` value.",
        },
        {"role": "user", "content": args},
    ]

    return create_chat_completion(model=model, messages=messages, temperature=0)


# Overly simple abstraction until we create something better
# simple retry mechanism when getting a rate error or a bad gateway
def create_chat_completion(
    messages: List[Message],  # type: ignore
    model: Optional[str] = None,
    temperature: float = CFG.temperature,
    max_tokens: Optional[int] = None,
) -> str:
    """Create a chat completion using the OpenAI API

    Args:
        messages (List[Message]): The messages to send to the chat completion
        model (str, optional): The model to use. Defaults to None.
        temperature (float, optional): The temperature to use. Defaults to 0.9.
        max_tokens (int, optional): The max tokens to use. Defaults to None.

    Returns:
        str: The response from the chat completion
    """
    num_retries = 10
    warned_user = False
    if CFG.debug_mode:
        print(
            f"{Fore.GREEN}Creating chat completion with model {model}, temperature {temperature}, max_tokens {max_tokens}{Fore.RESET}"
        )
    for plugin in CFG.plugins:
        if plugin.can_handle_chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            message = plugin.handle_chat_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if message is not None:
                return message
    response = None
    for attempt in range(num_retries):
        backoff = 2 ** (attempt + 2)
        try:
            if CFG.use_azure:
                response = api_manager.create_chat_completion(
                    deployment_id=CFG.get_azure_deployment_id_for_model(model),
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            elif CFG.use_local_model:
                tokenizer = AutoTokenizer.from_pretrained(model)
                inference_model = AutoModelForCausalLM.from_pretrained(model)
                #TODO: messages are of type List[Message], which is essentially List[Typed_dict]
                input_text = get_message_string(messages)
                inputs = tokenizer(input_text, return_tensors="pt", padding=False, truncation=True)
                kwargs = {"max_new_tokens": max_tokens, "eos_token_id": 50256, "pad_token_id": 50256}
                summ_tokens = inference_model.generate(inputs["input_ids"],
                                                       attention_mask=inputs["attention_mask"],
                                                       **kwargs)
                response = tokenizer.decode(summ_tokens[0])
            else:
                response = api_manager.create_chat_completion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            break
        except RateLimitError:
            if CFG.debug_mode:
                print(
                    f"{Fore.RED}Error: ", f"Reached rate limit, passing...{Fore.RESET}"
                )
            if not warned_user:
                logger.double_check(
                    f"Please double check that you have setup a {Fore.CYAN + Style.BRIGHT}PAID{Style.RESET_ALL} OpenAI API Account. "
                    + f"You can read more here: {Fore.CYAN}https://github.com/Significant-Gravitas/Auto-GPT#openai-api-keys-configuration{Fore.RESET}"
                )
                warned_user = True
        except (APIError, Timeout) as e:
            if e.http_status != 502:
                raise
            if attempt == num_retries - 1:
                raise
        if CFG.debug_mode:
            print(
                f"{Fore.RED}Error: ",
                f"API Bad gateway. Waiting {backoff} seconds...{Fore.RESET}",
            )
        time.sleep(backoff)
    if response is None:
        logger.typewriter_log(
            "FAILED TO GET RESPONSE FROM OPENAI",
            Fore.RED,
            "Auto-GPT has failed to get a response from OpenAI's services. "
            + f"Try running Auto-GPT again, and if the problem the persists try running it with `{Fore.CYAN}--debug{Fore.RESET}`.",
        )
        logger.double_check()
        if CFG.debug_mode:
            raise RuntimeError(f"Failed to get response after {num_retries} retries")
        else:
            quit(1)
    if CFG.use_local_model:
        resp = response
    else:
        resp = response.choices[0].message["content"]
    for plugin in CFG.plugins:
        if not plugin.can_handle_on_response():
            continue
        resp = plugin.on_response(resp)
    return resp


def get_message_string(messages: List[Message]) -> str:
    return ''.join("<|start|>{0}\n{1}<|end|>\n".format(m["role"], m["content"]) for m in messages)


def get_ada_embedding(text):
    text = text.replace("\n", " ")
    return api_manager.embedding_create(
        text_list=[text], model="text-embedding-ada-002"
    )


def create_embedding_with_ada(text) -> list:
    """Create an embedding with text-ada-002 using the OpenAI SDK"""
    num_retries = 10
    for attempt in range(num_retries):
        backoff = 2 ** (attempt + 2)
        try:
            return api_manager.embedding_create(
                text_list=[text], model="text-embedding-ada-002"
            )
        except RateLimitError:
            pass
        except (APIError, Timeout) as e:
            if e.http_status != 502:
                raise
            if attempt == num_retries - 1:
                raise
        if CFG.debug_mode:
            print(
                f"{Fore.RED}Error: ",
                f"API Bad gateway. Waiting {backoff} seconds...{Fore.RESET}",
            )
        time.sleep(backoff)
