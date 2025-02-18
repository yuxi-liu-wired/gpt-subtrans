import logging
logging.basicConfig(encoding='utf-8')
import re
from os import linesep
from PySubtitle.OpenAI.ChatGPTClient import ChatGPTClient
from PySubtitle.OpenAI.InstructGPTClient import InstructGPTClient

from PySubtitle.OpenAI.OpenAIClient import OpenAIClient
from PySubtitle.Translation import Translation
from PySubtitle.TranslationClient import TranslationClient
from PySubtitle.TranslationParser import TranslationParser
from PySubtitle.Options import Options
from PySubtitle.SubtitleBatch import SubtitleBatch

from PySubtitle.SubtitleError import TranslationAbortedError, TranslationError, TranslationFailedError, TranslationImpossibleError, UntranslatedLinesError
from PySubtitle.Helpers import BuildPrompt, Linearise, MergeTranslations, ParseSubstitutions, UnbatchScenes
from PySubtitle.SubtitleFile import SubtitleFile
from PySubtitle.SubtitleScene import SubtitleScene
from PySubtitle.TranslationEvents import TranslationEvents

class SubtitleTranslator:
    """
    Processes subtitles into scenes and batches and sends them for translation
    """
    def __init__(self, subtitles : SubtitleFile, options : Options):
        """
        Initialise a SubtitleTranslator with translation options
        """
        if not options:
            raise Exception("No translation options provided")

        self.subtitles = subtitles
        self.options = options
        self.events = TranslationEvents()
        self.aborted = False

        self.prompt = BuildPrompt(options)

        logging.debug(f"Translation prompt: {self.prompt}")
 
        # Update subtitle context from options and make our own copy of it
        self.context = subtitles.UpdateContext(options).copy()

        context_values = [f"{key}: {Linearise(value)}" for key, value in self.context.items()]
        logging.debug(f"Translation context:\n{linesep.join(context_values)}")

        # Initialise the client
        self.client = self._create_client(options, self.context)

    def StopTranslating(self):
        self.aborted = True
        self.client.AbortTranslation()

    def TranslateSubtitles(self):
        """
        Translate a SubtitleFile
        """
        options : Options = self.options
        subtitles : SubtitleFile = self.subtitles 

        if self.aborted:
            raise TranslationAbortedError()

        if not subtitles:
            raise TranslationError("No subtitles to translate")
    
        if subtitles.scenes and options.get('resume'):
            logging.info("Resuming translation")

        if not subtitles.scenes:
            if options.get('retranslate') or options.get('resume'):
                logging.warning(f"Previous subtitles not found, starting fresh...")

            self.subtitles.AutoBatch(options)

        if not subtitles.scenes:
            raise Exception("No scenes to translate")
        
        logging.info(f"Translating {subtitles.linecount} lines in {subtitles.scenecount} scenes")

        self.events.preprocessed(subtitles.scenes)

        max_lines = options.get('max_lines')
        remaining_lines = max_lines

        # Iterate over each subtitle scene and request translation
        for scene in subtitles.scenes:
            if self.aborted:
                raise TranslationAbortedError()

            if options.get('resume') and scene.all_translated:
                    logging.info(f"Scene {scene.number} already translated {scene.linecount} lines...")
                    continue

            logging.debug(f"Translating scene {scene.number} of {subtitles.scenecount}")
            batch_numbers = [ batch.number for batch in scene.batches if not batch.translated ] if options.get('resume') else None

            self.TranslateScene(scene, batch_numbers=batch_numbers, remaining_lines=remaining_lines)

            if remaining_lines:
                remaining_lines = max(0, remaining_lines - scene.linecount)
                if not remaining_lines:
                    logging.info(f"Reached max_lines limit of ({max_lines} lines)... finishing")
                    break

        # Linearise the translated scenes
        originals, translations, untranslated = UnbatchScenes(subtitles.scenes)

        if translations and not max_lines:
            logging.info(f"Successfully translated {len(translations)} lines!")

        if untranslated and not max_lines:
            logging.warning(f"Failed to translate {len(untranslated)} lines:")
            for line in untranslated:
                logging.info(f"Untranslated > {line.number}. {line.text}")

        subtitles.originals = originals
        subtitles.translated = translations

    def TranslateScene(self, scene : SubtitleScene, batch_numbers = None, line_numbers = None, remaining_lines=None):
        """
        Send a scene for translation
        """
        options : Options = self.options
        
        if not scene.context:
            scene.context = self.context.copy()
        else:
            scene.context = {**scene.context, **self.context}

        try:
            if batch_numbers:
                batches = [ batch for batch in scene.batches if batch.number in batch_numbers ]
            else:
                batches = scene.batches

            context = scene.context.copy()
            context['scene'] = f"Scene {scene.number}: {scene.summary}" if scene.summary else f"Scene {scene.number}"

            self.TranslateBatches(self.client, batches, line_numbers, context, remaining_lines)

            # Update the scene summary based on the best available information (we hope)
            scene.summary = self.SanitiseSummary(scene.summary) or self.SanitiseSummary(context.get('scene')) or self.SanitiseSummary(context.get('summary'))

            # Notify observers the scene was translated
            self.events.scene_translated(scene)

        except TranslationAbortedError:
            raise

        except Exception as e:
            if options.get('stop_on_error'):
                raise
            else:
                logging.warning(f"Failed to translate scene {scene.number} ({str(e)})... finishing")

    def TranslateBatches(self, client : TranslationClient, batches : list[SubtitleBatch], line_numbers : list[int], context : dict, remaining_lines=None):
        """
        Send batches of subtitles for translation, building up context.
        """
        options : Options = self.options

        substitutions = ParseSubstitutions(context.get('substitutions', {}))
        match_partial_words = options.get('match_partial_words')
        max_context_summaries = options.get('max_context_summaries')

        client = self.client

        for batch in batches:
            if self.aborted:
                raise TranslationAbortedError()

            if options.get('resume') and batch.all_translated:
                logging.info(f"Scene {batch.scene} batch {batch.number} already translated {batch.size} lines...")
                continue

            if batch.context and (options.get('retranslate') or options.get('reparse')):
                # If it's a retranslation, restore context from the batch
                context = {**context, **batch.context}

            # Apply any substitutions to the input
            replacements = batch.PerformInputSubstitutions(substitutions, match_partial_words)

            # Split single lines with blocks of whitespace
            if options.get('whitespaces_to_newline'):
                batch.ConvertWhitespaceBlocksToNewlines()

            # Filter out empty lines
            originals = [line for line in batch.originals if line.text and line.text.strip()]

            if remaining_lines and len(originals) > remaining_lines:
                logging.info("Truncating batch to remain within max_lines")
                originals = originals[:remaining_lines]

            try:
                if  options.get('reparse') and batch.translation:
                    logging.info(f"Reparsing scene {batch.scene} batch {batch.number} with {len(originals)} lines...")
                    translation = batch.translation
                else:
                    logging.debug(f"Translating scene {batch.scene} batch {batch.number} with {len(originals)} lines...")

                    if replacements:
                        replaced = [f"{Linearise(k)} -> {Linearise(v)}" for k,v in replacements.items()]
                        logging.info(f"Made substitutions in input:\n{linesep.join(replaced)}")

                    if options.get('preview'):
                        self.events.batch_translated(batch)
                        continue

                    # Build summaries context
                    context['summaries'] = self.subtitles.GetBatchContext(batch.scene, batch.number, max_context_summaries)
                    context['summary'] = batch.summary
                    context['batch'] = f"Scene {batch.scene} batch {batch.number}"

                    # Ask the client to do the translation
                    translation : Translation = client.RequestTranslation(self.prompt, originals, context)

                    if self.aborted:
                        raise TranslationAbortedError()

                    if translation.quota_reached:
                        raise TranslationImpossibleError("OpenAI account quota reached, please upgrade your plan or wait until it renews", translation)

                    if translation.reached_token_limit:
                        # Try again without the context to keep the tokens down
                        logging.warning("Hit API token limit, retrying batch without context...")
                        translation = client.RequestTranslation(self.prompt, originals, None)

                        if translation.reached_token_limit:
                            raise TranslationError(f"Too many tokens in translation", translation)

                if translation:
                    translation.ParseResponse()

                    batch.translation = translation
                    batch.AddContext('summary', context.get('summary'))
                    batch.AddContext('summaries', context.get('summaries'))

                    # Process the response
                    self.ProcessTranslation(batch, line_numbers, context, client)

                else:
                    logging.warning(f"No translation for scene {batch.scene} batch {batch.number}")

            except TranslationAbortedError:
                raise
                    
            except TranslationError as e:
                if options.get('stop_on_error') or isinstance(e, TranslationImpossibleError):
                    raise TranslationFailedError(f"Failed to translate a batch... terminating", batch.translation, e)
                else:
                    logging.warning(f"Error translating batch: {str(e)}")

            if remaining_lines:
                remaining_lines = max(0, remaining_lines - len(originals))
                if not remaining_lines:
                    break

            context['previous_batch'] = batch

            # Notify observers the batch was translated
            self.events.batch_translated(batch)

    def ProcessTranslation(self, batch : SubtitleBatch, line_numbers : list[int], context : dict, client : TranslationClient):
        """
        Attempt to extract translation from the API response
        """
        options : Options = self.options
        substitutions = options.get('substitutions')
        match_partial_words = options.get('match_partial_words')

        translation : Translation = batch.translation

        if not translation.has_translation:
            raise ValueError("Translation contains no translated text")
        
        logging.debug(f"Scene {batch.scene} batch {batch.number} translation:\n{translation.text}\n")

        try:
            # Apply the translation to the subtitles
            parser : TranslationParser = client.GetParser()
            
            # Reset error list, hopefully they're obsolete
            batch.errors = []

            try:
                parser.ProcessTranslation(translation)

                # Try to match the translations with the original lines
                translated, unmatched = parser.MatchTranslations(batch.originals)

                if unmatched:
                    logging.warning(f"Unable to match {len(unmatched)} lines with a source line")
                    if options.get('enforce_line_parity'):
                        raise UntranslatedLinesError(f"No translation found for {len(unmatched)} lines", unmatched)

                # Sanity check the results
                parser.ValidateTranslations()
            
            except TranslationAbortedError:
                raise

            except TranslationError as e:
                if not options.get('allow_retranslations'):
                    raise
                else:
                    batch.errors.append(e)

            # Consider retrying if there were errors
            if batch.errors and options.get('allow_retranslations') and not self.aborted:
                logging.warn(f"Scene {batch.scene} batch {batch.number} failed validation, requesting retranslation")
                retranslated = self.RequestRetranslations(client, batch, translation)

                translated = MergeTranslations(translated or [], retranslated)

            # Assign the translated lines to the batch
            if line_numbers:
                translated = [line for line in translated if line.number in line_numbers]
                batch.translated = MergeTranslations(batch.translated or [], translated)
            else:
                batch.translated = translated

            if batch.untranslated:
                batch.AddContext('untranslated_lines', [f"{item.number}. {item.text}" for item in batch.untranslated])

            # Apply any word/phrase substitutions to the translation 
            replacements = batch.PerformOutputSubstitutions(substitutions, match_partial_words)

            if replacements:
                replaced = [f"{k} -> {v}" for k,v in replacements.items()]
                logging.info(f"Made substitutions in output:\n{linesep.join(replaced)}")

            # Perform substitutions on the output
            translation.PerformSubstitutions(substitutions, match_partial_words)

            # Update the context, unless it's a retranslation pass
            if not options.get('retranslate'):
                batch.summary = self.SanitiseSummary(translation.summary or batch.summary)
                scene_summary = self.SanitiseSummary(translation.scene)

                context['summary'] = batch.summary
                context['scene'] = scene_summary or context['scene']
                context['synopsis'] = translation.synopsis or context.get('synopsis', "")
                #context['names'] = translation.names or context.get('names', []) or options.get('names')
                batch.UpdateContext(context)

            logging.info(f"Scene {batch.scene} batch {batch.number}: {len(batch.translated or [])} lines and {len(batch.untranslated or [])} untranslated.")

            if batch.summary and batch.summary.strip():
                logging.info(f"Summary: {batch.summary}")

        except TranslationError as te:
            if options.get('stop_on_error'):
                raise
            else:
                logging.warning(f"Error translating batch: {str(te)}")


    def RequestRetranslations(self, client : TranslationClient, batch : SubtitleBatch, translation : str):
        """
        Ask the client to retranslate the input and correct errors
        """
        retranslation : Translation = client.RequestRetranslation(translation, batch.errors)

        if not isinstance(retranslation, Translation):
            raise TranslationError("Retranslation is not the expected type")

        logging.debug(f"Scene {batch.scene} batch {batch.number} retranslation:\n{retranslation.text}\n")

        parser : TranslationParser = client.GetParser()

        retranslated = parser.ProcessTranslation(retranslation)

        if not retranslated:
            #TODO line-by-line retranslation? Automatic batch splitting?
            logging.error("Retranslation request did not produce a useful result")
            return []
        
        try:
            batch.errors = []

            _, unmatched = parser.MatchTranslations(batch.originals)

            if unmatched:
                logging.warning(f"Still unable to match {len(unmatched)} lines with a source line - try splitting the batch")
                batch.errors.append(UntranslatedLinesError(f"No translation found for {len(unmatched)} lines", unmatched))

            parser.ValidateTranslations()

            logging.info("Retranslation passed validation")

        except TranslationError as e:
            logging.warn(f"Retranslation request did not fix problems:\n{retranslation.text}\n")

        return retranslated

    def SanitiseSummary(self, summary : str):
        if not summary:
            return None

        summary = re.sub(r'^(?:(?:Scene|Batch)[\s\d:\-]*)+', '', summary, flags=re.IGNORECASE)
        summary = summary.replace("Summary of the batch", "")
        summary = summary.replace("Summary of the scene", "")

        movie_name = self.options.get('movie_name')
        if movie_name:
            # Remove movie name and any connectors (-,: or whitespace)
            summary = re.sub(r'^' + re.escape(movie_name) + r'\s*[:\-]\s*', '', summary)

        return summary.strip() if summary.strip() else None

    def _create_client(self, options, context):
        """ Create an appropriate client for the model (TODO: client registration by regex) """
        model = options.get('model') or options.get('gpt_model')

        if model.startswith("gpt"):
            if model.find("instruct") >= 0:
                return InstructGPTClient(options, context.get('instructions'))
            else:
                return ChatGPTClient(options, context.get('instructions'))
        else:
            raise Exception("Model not supported (yet)")

    @classmethod
    def GetAvailableModels(cls, api_key : str, api_base : str):
        """
        Returns a list of possible values for the LLM model 
        """
        #TODO - parameterise the client/endpoint
        return OpenAIClient.GetAvailableModels(api_key, api_base)

