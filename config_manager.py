from dataclasses import dataclass, field
from typing import Any, NamedTuple, Type, Union
import inspect
import types
import abc
import os
import json
import enum

import logging
_logger = logging.getLogger("Config Manager")

JSON_LIST = list['JSON_ITEM']
JSON_OBJ = dict[str, 'JSON_ITEM']
JSON_ITEM = str | int | float | JSON_LIST | JSON_OBJ

FILE_ID = str | int
ITEM_ID = str | int

class I_ConfigSerializable(abc.ABC):
    @abc.abstractmethod
    def config_serialize(self, item_id:ITEM_ID) -> JSON_OBJ: ...

class I_ConfigDeserializable(abc.ABC):
    @abc.abstractmethod
    def config_reconfig(self, item_id:ITEM_ID, json_obj:JSON_OBJ, **kwargs) -> None: ...

class I_ConfigItem(I_ConfigDeserializable, I_ConfigSerializable): ...

class I_ConfigItemFactory(abc.ABC):
    @staticmethod
    @abc.abstractmethod
    def config_deserialize(item_id:ITEM_ID, json_obj:JSON_OBJ|None, **kwargs) -> I_ConfigSerializable: ...

CONFIG_ITEM_LIKE = Union['ConfigItemTemplate', 'ConfigTarget', I_ConfigItem, tuple[Union['ConfigTarget', I_ConfigItem], dict[str, Any]]]
CONFIG_ITEMS_LIKE = dict[ITEM_ID, CONFIG_ITEM_LIKE]

class ConfigTarget(NamedTuple):
    obj:object|dict
    attr:str
    factory:I_ConfigItemFactory

@dataclass
class ConfigItemTemplate:
    target:I_ConfigItem|ConfigTarget = field()
    default_args:list[Any] = field(default_factory=list)
    default_kwargs:dict[str, Any] = field(default_factory=dict)

    def _serialize_obj(self, item_id:ITEM_ID) -> JSON_OBJ:
        _target = None
        if type(self.target) == ConfigTarget:
            if type(self.target.obj) == dict:
                _target:I_ConfigSerializable = self.target.obj.get(self.target.attr, None)
            else:
                _target:I_ConfigSerializable = getattr(self.target.obj, self.target.attr, None)
            if _target == None:
                # TODO: Figure out what to do here
                raise RuntimeError("Can't serialize non existent object")
        else:
            _target = self.target

        if not issubclass(type(_target), I_ConfigSerializable):
            raise RuntimeError("Can't serialize object that dose not inherit from I_ConfigSerializable")
        return _target.config_serialize(item_id)

    def _deserialize_obj(self, item_id:ITEM_ID, json_obj:JSON_OBJ|None) -> None:
        _args = self.default_args
        _kwargs = self.default_kwargs
        if type(self.target) == ConfigTarget:
            if type(self.target.obj) == dict:
                if (attr_obj := self.target.obj.get(self.target.attr, None)) != None:
                    _logger.warning(f"Overwriting preexisting value ({attr_obj}) in the target (dict:{self.target.obj}, key:\"{self.target.attr}\").")
                self.target.obj[self.target.attr] = self.target.factory.config_deserialize(item_id, json_obj, *_args, **_kwargs)
            else:
                if (attr_obj := getattr(self.target.obj, self.target.attr, None)) != None:
                    _logger.warning(f"Overwriting preexisting value ({attr_obj}) in the target (obj:{self.target.obj}, attr:\"{self.target.attr}\").")
                setattr(self.target.obj, self.target.attr, self.target.factory.config_deserialize(item_id, json_obj, *_args, **_kwargs))
        elif json_obj != None:
            self.target.config_reconfig(item_id, json_obj, *_args, **_kwargs)
        
@dataclass
class ConfigFileTemplate:
    file_path:str|None = field(default=None)
    file_required:bool = field(default=False)
    item_templates:dict[ITEM_ID, ConfigItemTemplate] = field(default_factory=dict)

class ConfigManager:
    def __init__(self) -> None:
        self._file_templates:dict[FILE_ID, ConfigFileTemplate] = {}
        self._finalized = False

    def add_file_path(self, file_id:FILE_ID|None, file_path:str, file_required:bool=False) -> FILE_ID|None:
        # Get File Template instance for the given id
        file_template = self._file_templates.setdefault(file_id, ConfigFileTemplate(file_path=None))
        self._finalized = False

        # Attempt to set file templates path
        if file_template.file_path != None:
            raise RuntimeError(f"Attempted to assign a path to the File Template ({file_id}) more than once.")
        file_template.file_path = file_path
        file_template.file_required = file_required

    def add_items(self, file_id:FILE_ID, items:CONFIG_ITEMS_LIKE) -> None:
        # Get File Template instate for the given id
        file_template = self._file_templates.setdefault(file_id, ConfigFileTemplate(item_templates={}))
        self._finalized = False

        # Attempt to add Items to selected template
        for item_id, item_like in items.items():
            # Check item template dose not already exist
            if item_id in file_template.item_templates.keys():
                raise RuntimeError(f"Attempted to add a Item ({item_id}) multiple time to the File Template ({file_id}).")
            
            # Add Item_Like as appropriate template
            item_type = type(item_like)
            if item_type == ConfigItemTemplate:
                file_template.item_templates[item_id] = item_like
            elif item_type == ConfigTarget or issubclass(item_type, I_ConfigItem):
                file_template.item_templates[item_id] = ConfigItemTemplate(item_like)
            elif item_type == tuple and (type(item_like[0]) == ConfigTarget or issubclass(type(item_like[0]), I_ConfigItem)):
                if len(item_like) > 1 and type(item_like[1]) != list:
                    raise RuntimeError(f"Can't setup kwargs using {type(item_like[1])} must be type <list[any]>")
                if len(item_like) > 2 and type(item_like[2]) != dict:
                    raise RuntimeError(f"Can't setup kwargs using {type(item_like[1])} must be type <dict[str, any]>")
                file_template.item_templates[item_id] = ConfigItemTemplate(*item_like)
            else:
                raise RuntimeError(f"Unsupported CONFIG_ITEM_LIKE type, {item_type} encountered.")

    def finalize_layout(self) -> None:
        # Check that a file path has been set for all templates
        query_result = [file_id for file_id, file_template in self._file_templates.items() if file_template.file_path == None]
        if len(query_result) > 0:
            raise RuntimeError(f"Failed to finalize Config Layout due to missing file paths for the following File Ids {query_result}.")

        # Set Finalized flag
        self._finalized = True

    def load_configs(self) -> None:
        # Check if layout was finalized
        if not self._finalized:
            raise RuntimeError("Attempted to load Configs before finalizing layouts (finalize_layout).")

        # Attempt to Load in templates
        for file_id, file_template in self._file_templates.items():
            self._load_config_file(file_id, file_template)

    def load_config_file(self, file_id:FILE_ID) -> None:
        # Check if layout was finalized
        if not self._finalized:
            raise RuntimeError("Attempted to load Configs before finalizing layouts (finalize_layout).")
        
        file_template = self._file_templates[file_id]
        self._load_config_file(file_id, file_template)

    def _load_config_file(self, file_id:FILE_ID, file_template:ConfigFileTemplate) -> None:
        if not os.path.isfile(file_template.file_path):
            # Check if file is required to exist
            if file_template.file_required:
                raise RuntimeError(f"Required Config File Template ({file_id}) could not be found at \"{file_template.file_path}\".")
            
            # Create default items
            for item_id, item_template in file_template.item_templates.items():
                item_template._deserialize_obj(item_id, None)
        else:
            # Load JSON File
            json_data = None
            with open(file_template.file_path) as json_file:
                json_data = json.load(json_file)

            # Confirm file properties
            file_properties = json_data["properties"]
            if file_properties["id"] != file_id:
                raise RuntimeError("Attempted to load file with id not matching given id")

            # Load Items from JSON data
            file_items:JSON_OBJ = json_data["general_items"]
            for item_id, item_template in file_template.item_templates.items():
                json_obj = file_items.get(item_id, None)
                item_template._deserialize_obj(item_id, json_obj)

    def save_configs(self) -> None:
        # Check if layout was finalized
        if not self._finalized:
            raise RuntimeError("Attempted to Save Configs before finalizing layouts (finalize_layout).")
        
        # Attempt to Save out templates
        for file_id, file_template in self._file_templates.items():
            self._save_config_file(file_id, file_template)

    def save_config_file(self, file_id:FILE_ID) -> None:
        # Check if layout was finalized
        if not self._finalized:
            raise RuntimeError("Attempted to Save Configs before finalizing layouts (finalize_layout).")
        
        file_template = self._file_templates[file_id]
        self._save_config_file(file_id, file_template)
    
    def _save_config_file(self, file_id:FILE_ID, file_template:ConfigFileTemplate) -> None:
        json_data = {}
            
        # Save File properties
        json_data["properties"] = {"id":file_id}
        
        # Save Items
        json_items = {}
        json_data["general_items"] = json_items
        for item_id, item_template in file_template.item_templates.items():
            json_items[item_id] = item_template._serialize_obj(item_id)
    
        # Save out file
        with open(file_template.file_path, 'w') as json_file:
            json.dump(json_data, json_file, indent=2)

def apply_attributes(obj:object, attributes:dict[str,any]) -> None:
    for attr, val in attributes.items():
        setattr(obj, attr, val)
