import logging
logging.basicConfig(encoding='utf-8')
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QDialogButtonBox, QFormLayout, QFrame, QLabel)
from GUI.GuiHelpers import GetInstructionFiles, LoadInstructionsResource

from GUI.Widgets.OptionsWidgets import CreateOptionWidget
from PySubtitle import SubtitleProject
from PySubtitle.SubtitleBatcher import CreateSubtitleBatcher
from PySubtitle.SubtitleScene import SubtitleScene
from PySubtitle.SubtitleTranslator import SubtitleTranslator

class NewProjectSettings(QDialog):
    SETTINGS = {
        'target_language': (str, "Language to translate the subtitles to"),
        'model': (str, "AI model to use as the translator"),
        'min_batch_size': (int, "Fewest lines to send in separate batch"),
        'max_batch_size': (int, "Most lines to send in each batch"),
        'scene_threshold': (float, "Number of seconds gap to consider it a new scene"),
        'use_simple_batcher': (bool, "Use old batcher instead of batching dynamically based on gap size"),
        'batch_threshold': (float, "Number of seconds gap to consider starting a new batch (simple batcher)"),
        'gpt_prompt': (str, "High-level instructions for the translator"),
        'instruction_file': (str, "Detailed instructions for the translator")
    }

    def __init__(self, project : SubtitleProject, parent=None):
        super(NewProjectSettings, self).__init__(parent)
        self.setWindowTitle("Project Settings")
        self.setMinimumWidth(800)

        self.fields = {}

        self.project : SubtitleProject = project
        self.settings : dict = project.options.GetSettings()
        self.settings['model'] = self.settings.get('model') or self.settings.get('gpt_model')

        api_key = project.options.api_key()
        api_base = project.options.api_base()

        if api_key:
            models = SubtitleTranslator.GetAvailableModels(api_key, api_base)
            self.SETTINGS['model'] = (models, self.SETTINGS['model'][1])

        instruction_files = GetInstructionFiles()
        if instruction_files:
            self.SETTINGS['instruction_file'] = (instruction_files, self.SETTINGS['instruction_file'][1])

        settings_widget = QFrame(self)

        self.form_layout = QFormLayout(settings_widget)
        self.form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        for key, setting in self.SETTINGS.items():
            key_type, tooltip = setting
            field = CreateOptionWidget(key, self.settings[key], key_type, tooltip=tooltip)
            field.contentChanged.connect(self._preview_batches)
            self.form_layout.addRow(field.name, field)
            self.fields[key] = field

        self.layout = QVBoxLayout(self)
        self.layout.addWidget(settings_widget)

        self.preview_widget = QLabel(self)
        self.layout.addWidget(self.preview_widget)

        self.buttonBox = QDialogButtonBox(QDialogButtonBox.Ok, self)
        self.buttonBox.accepted.connect(self.accept)
        self.layout.addWidget(self.buttonBox)

        self.fields['instruction_file'].contentChanged.connect(self._update_instruction_file)

        self._preview_batches()

    def accept(self):
        try:
            self._update_settings()

            instructions_file = self.settings.get('instruction_file')
            if instructions_file:
                logging.info(f"Project instructions set from {instructions_file}")
                try:
                    instructions = LoadInstructionsResource(instructions_file)

                    self.settings['prompt'] = instructions.prompt
                    self.settings['instructions'] = instructions.instructions
                    self.settings['retry_instructions'] = instructions.retry_instructions
                    # Legacy
                    self.settings['gpt_prompt'] = instructions.prompt

                    logging.debug(f"Prompt: {instructions.prompt}")
                    logging.debug(f"Instructions: {instructions.instructions}")

                except Exception as e:
                    logging.error(f"Unable to load instructions from {instructions_file}: {e}")

            self.project.UpdateProjectSettings(self.settings)

        except Exception as e:
            logging.error(f"Unable to update settings: {e}")

        super(NewProjectSettings, self).accept()

    def _update_settings(self):
        layout = self.form_layout.layout()

        for row in range(layout.rowCount()):
            field = layout.itemAt(row, QFormLayout.FieldRole).widget()
            self.settings[field.key] = field.GetValue()

    def _preview_batches(self):
        self._update_settings()
        self._update_inputs()

        batcher = CreateSubtitleBatcher(self.settings)
        if batcher.min_batch_size < batcher.max_batch_size:
            scenes : list[SubtitleScene] = batcher.BatchSubtitles(self.project.subtitles.originals)
            batch_count = sum(scene.size for scene in scenes)
            line_count = sum(scene.linecount for scene in scenes)
            self.preview_widget.setText(f"{line_count} lines in {len(scenes)} scenes and {batch_count} batches")

    def _update_inputs(self):
        layout : QFormLayout = self.form_layout.layout()

        for row in range(layout.rowCount()):
            field = layout.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
            if field.key == 'batch_threshold':
                use_simple_batcher = self.settings.get('use_simple_batcher')
                field.setEnabled(use_simple_batcher)

    def _update_instruction_file(self):
        """ Update the prompt when the instruction file is changed """
        instruction_file = self.fields['instruction_file'].GetValue()
        if instruction_file:
            try:
                instructions = LoadInstructionsResource(instruction_file)
                self.fields['gpt_prompt'].SetValue(instructions.prompt)
            except Exception as e:
                logging.error(f"Unable to load instructions from {instruction_file}: {e}")
