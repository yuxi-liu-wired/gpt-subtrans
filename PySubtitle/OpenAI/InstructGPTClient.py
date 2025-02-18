import logging
logging.basicConfig(encoding='utf-8')
import time
import openai

from PySubtitle.Helpers import ParseDelayFromHeader
from PySubtitle.OpenAI.OpenAIClient import OpenAIClient
from PySubtitle.SubtitleError import NoTranslationError, TranslationAbortedError, TranslationImpossibleError

linesep = '\n'

class InstructGPTClient(OpenAIClient):
    """
    Handles communication with GPT instruct models to request translations
    """
    def SupportedModels(self):
        models = OpenAIClient.GetAvailableModels(self.options.api_key, self.options.api_base)
        return [ model for model in models if model.find("instruct") >= 0]

    def _send_messages(self, messages : list, temperature : float = None):
        """
        Make a request to the OpenAI API to provide a translation
        """
        options = self.options
        max_tokens = options.get('max_instruct_tokens', 2048)
        max_retries = options.get('max_retries', 3.0)
        backoff_time = options.get('backoff_time', 5.0)
        model = options.get('gpt_model')
        temperature = temperature or options.get('temperature', 0.0)

        translation = {}
        retries = 0

        prompt = self._build_prompt(messages)

        while retries <= max_retries and not self.aborted:
            try:
                response = self.client.completions.create(
                    model=model,
                    prompt=prompt,
                    temperature=temperature,
                    n=1,
                    max_tokens=max_tokens
                )

                if self.aborted:
                    raise TranslationAbortedError()

                translation['response_time'] = getattr(response, 'response_ms', 0)

                if response.usage:
                    translation['prompt_tokens'] = getattr(response.usage, 'prompt_tokens')
                    translation['completion_tokens'] = getattr(response.usage, 'completion_tokens')
                    translation['total_tokens'] = getattr(response.usage, 'total_tokens')

                # We only expect one choice to be returned as we have 0 temperature
                if response.choices:
                    choice = response.choices[0]
                    if not isinstance(choice.text, str):
                        raise NoTranslationError("Instruct model completion text is not a string")

                    translation['finish_reason'] = getattr(choice, 'finish_reason', None)
                    translation['text'] = choice.text
                else:
                    raise NoTranslationError("No choices returned in the response", response)

                # Return the response if the API call succeeds
                return translation
            
            except openai.RateLimitError as e:
                retry_after = e.response.headers.get('x-ratelimit-reset-requests') or e.response.headers.get('Retry-After')
                if retry_after:
                    retry_seconds = ParseDelayFromHeader(retry_after)
                    logging.warning(f"Rate limit hit, retrying in {retry_seconds} seconds...")
                    time.sleep(retry_seconds)
                    continue
                else:
                    logging.warning("Rate limit hit, quota exceeded. Please wait until the quota resets.")
                    raise

            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                if self.aborted:
                    raise TranslationAbortedError()
                
                if isinstance(e, openai.APIConnectionError):
                    raise TranslationImpossibleError(str(e), translation)
                elif retries == max_retries:
                    logging.warning(f"OpenAI failure {str(e)}, aborting after {retries} retries...")
                    raise
                else:
                    retries += 1
                    sleep_time = backoff_time * 2.0**retries
                    logging.warning(f"OpenAI error {str(e)}, retrying in {sleep_time}...")
                    time.sleep(sleep_time)
                    continue

            except Exception as e:
                raise TranslationImpossibleError(f"Unexpected error communicating with OpenAI", translation, error=e)

        return None

    def _build_prompt(self, messages : list):
        return "\n\n".join([ f"#{m.get('role')} ###\n{m.get('content')}" for m in messages ])