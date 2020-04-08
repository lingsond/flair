import os
import shutil
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from functools import cmp_to_key
from itertools import combinations
from operator import attrgetter
from pathlib import Path
from typing import Union, Callable, Dict, List, Tuple, Iterable, IO

from lxml import etree

import flair
from flair.datasets import ColumnCorpus
from flair.file_utils import cached_path, unzip_file, unzip_targz_file


class Entity:
    def __init__(self, char_span: Tuple[int, int], entity_type: str):
        self.char_span = range(*char_span)
        self.type = entity_type

    def __str__(self):
        return (
            self.type
            + "("
            + str(self.char_span[0])
            + ","
            + str(self.char_span[1])
            + ")"
        )

    def __repr__(self):
        return str(self)

    def is_before(self, other_entity) -> bool:
        """
        Checks whether this entity is located before the given one

        :param other_entity: Entity to check
        """
        return self.char_span.stop <= other_entity.char_span.start

    def contains(self, other_entity) -> bool:
        """
        Checks whether the given entity is fully contained in this entity

        :param other_entity: Entity to check
        """
        return (
            other_entity.char_span.start >= self.char_span.start
            and other_entity.char_span.stop <= self.char_span.stop
        )

    def overlaps(self, other_entity) -> bool:
        """
        Checks whether this and the given entity overlap

        :param other_entity: Entity to check
        """
        return (
            self.char_span.start <= other_entity.char_span.start < self.char_span.stop
        ) or (self.char_span.start < other_entity.char_span.stop <= self.char_span.stop)


class NestedEntity(Entity):
    def __init__(
        self,
        char_span: Tuple[int, int],
        entity_type: str,
        nested_entities: Iterable[Entity],
    ):
        super(NestedEntity, self).__init__(char_span, entity_type)
        self.nested_entities = nested_entities


class InternalBioNerDataset:
    def __init__(
        self, documents: Dict[str, str], entities_per_document: Dict[str, List[Entity]]
    ):
        self.documents = documents
        self.entities_per_document = entities_per_document


def overlap(entity1, entity2):
    return range(max(entity1[0], entity2[0]), min(entity1[1], entity2[1]))


def compare_by_start_and_length(entity1, entity2):
    start_offset = entity1.char_span.start - entity2.char_span.start
    return (
        start_offset
        if start_offset != 0
        else len(entity2.char_span) - len(entity1.char_span)
    )


def merge_overlapping_entities(entities):
    entities = list(entities)

    entity_set_stable = False
    while not entity_set_stable:
        for e1, e2 in combinations(entities, 2):
            if overlap(e1, e2):
                merged_entity = (min(e1[0], e2[0]), max(e1[1], e2[1]))
                entities.remove(e1)
                entities.remove(e2)
                entities.append(merged_entity)
                break
        else:
            entity_set_stable = True

    return entities


def merge_datasets(data_sets: Iterable[InternalBioNerDataset]):
    all_documents = {}
    all_entities = {}

    for ds in data_sets:
        all_documents.update(ds.documents)
        all_entities.update(ds.entities_per_document)

    return InternalBioNerDataset(
        documents=all_documents, entities_per_document=all_entities
    )


def filter_entities(
    dataset: InternalBioNerDataset, target_types: Iterable[str]
) -> InternalBioNerDataset:
    """
    FIXME Map to canonical type names
    """
    target_entities_per_document = {
        id: [e for e in entities if e.type in target_types]
        for id, entities in dataset.entities_per_document.items()
    }

    return InternalBioNerDataset(
        documents=dataset.documents, entities_per_document=target_entities_per_document
    )


def find_overlapping_entities(
    entities: Iterable[Entity],
) -> List[Tuple[Entity, Entity]]:
    # Sort the entities by their start offset
    entities = sorted(entities, key=lambda e: e.char_span.start)

    overlapping_entities = []
    for i in range(0, len(entities)):
        current_entity = entities[i]
        for other_entity in entities[i + 1 :]:
            if current_entity.overlaps(other_entity):
                # Entities overlap!
                overlapping_entities.append((current_entity, other_entity))
            else:
                # Second entity is located after the current one!
                break

    return overlapping_entities


def find_nested_entities(entities: Iterable[Entity]) -> List[NestedEntity]:
    # Sort entities by start offset and length (i.e. rank longer entity spans first)
    entities = sorted(entities, key=cmp_to_key(compare_by_start_and_length))

    # Initial list with entities and whether they are already contained in a nested entity
    entities = [(entity, False) for entity in entities]

    nested_entities = []
    for i in range(0, len(entities)):
        current_entity, is_part_of_other_entity = entities[i]
        if is_part_of_other_entity:
            continue

        contained_entities = []
        for j in range(i + 1, len(entities)):
            other_entity, _ = entities[j]

            if current_entity.is_before(other_entity):
                # other_entity is located after the current one
                break

            elif current_entity.contains(other_entity):
                # other_entity is contained in current_entity
                contained_entities.append(other_entity)
                entities[j] = (other_entity, True)

        if len(contained_entities) > 0:
            nested_entities.append(
                NestedEntity(
                    (current_entity.char_span.start, current_entity.char_span.stop),
                    current_entity.type,
                    contained_entities,
                )
            )

    return nested_entities


def normalize_entity_spans(entities: Iterable[Entity]) -> List[Entity]:
    # Sort entities by start offset and length (i.e. rank longer entity spans first)
    entities = sorted(entities, key=cmp_to_key(compare_by_start_and_length))

    for i in range(0, len(entities)):
        current_entity = entities[i]
        if current_entity is None:
            continue

        contained_entities = []
        for j in range(i + 1, len(entities)):
            other_entity = entities[j]
            if other_entity is None:
                continue

            if current_entity.is_before(other_entity):
                # other_entity is located after the current one
                break

            elif current_entity.contains(other_entity):
                # other entity is nested in the current one
                contained_entities.append((other_entity, j))

            elif current_entity.overlaps(other_entity):
                # Shift overlapping entities
                shifted_entity = Entity(
                    (current_entity.char_span.stop, other_entity.char_span.stop),
                    other_entity.type,
                )
                entities[j] = shifted_entity

        if len(contained_entities) == 1:
            # Only one smaller entity span is contained -> take the longer one and erase the shorter one
            contained_entity, position = contained_entities[0]
            entities[position] = None

        elif len(contained_entities) > 1:
            # Wrapper for sorting entries by start offset and length
            def compare_entries(entry1, entry2):
                return compare_by_start_and_length(entry1[0], entry2[0])

            contained_entities = sorted(
                contained_entities, key=cmp_to_key(compare_entries)
            )

            # Keep first nested entity
            current_contained_entity = contained_entities[0][0]

            # Fill the complete span successively with non-overlapping entities
            for other_contained_entity, position in contained_entities[1:]:
                if current_contained_entity.is_before(other_contained_entity):
                    current_contained_entity = other_contained_entity
                else:
                    # Entities overlap - erase other contained entity!
                    # FIXME: Shift overlapping entity alternatively?
                    entities[position] = None

            # Erase longer entity
            entities[i] = None

    return [entity for entity in entities if entity is not None]


class CoNLLWriter:
    def __init__(
        self,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]],
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]],
    ):
        """
        :param tokenizer: Callable that segments a sentence into words
        :param sentence_splitter: Callable that segments a document into sentences
        """
        self.tokenizer = tokenizer
        self.sentence_splitter = sentence_splitter

    def process_dataset(
        self, datasets: Dict[str, InternalBioNerDataset], out_dir: Path
    ):
        self.write_to_conll(datasets["train"], out_dir / "train.conll")
        self.write_to_conll(datasets["dev"], out_dir / "dev.conll")
        self.write_to_conll(datasets["test"], out_dir / "test.conll")

    def write_to_conll(self, dataset: InternalBioNerDataset, output_file: Path):
        os.makedirs(output_file.parent, exist_ok=True)

        with output_file.open("w") as f:
            for document_id in dataset.documents.keys():
                document_text = dataset.documents[document_id]
                sentences, sentence_offsets = self.sentence_splitter(document_text)
                entities = deque(sorted(dataset.entities_per_document[document_id],
                                  key=attrgetter('char_span.start', 'char_span.stop')))

                current_entity = entities.popleft()
                in_entity = False
                for sentence, sentence_offset in zip(sentences, sentence_offsets):
                    tokens, token_offsets = self.tokenizer(sentence)
                    for token, token_offset in zip(tokens, token_offsets):
                        offset = sentence_offset + token_offset

                        if current_entity and offset >= current_entity.char_span.stop:
                            in_entity = False
                            if entities:
                                current_entity = entities.popleft()
                            else:
                                current_entity = None

                        # FIXME This assumes that entities aren't nested, we have to ensure that beforehand
                        if current_entity and offset in current_entity.char_span:
                            if not in_entity:
                                tag = "B-" + current_entity.type
                                in_entity = True
                            else:
                                tag = "I-" + current_entity.type
                        else:
                            tag = "O"
                            in_entity = False

                        f.write(" ".join([token, tag]) + "\n")
                    f.write("\n")


def whitespace_tokenize(text):
    offset = 0
    tokens = []
    offsets = []
    for token in text.split():
        tokens.append(token)
        offsets.append(offset)
        offset += len(token) + 1

    return tokens, offsets


class SciSpacyTokenizer:
    def __init__(self):
        import spacy

        self.nlp = spacy.load(
            "en_core_sci_sm", disable=["tagger", "ner", "parser", "textcat"]
        )

    def __call__(self, sentence: str):
        sentence = self.nlp(sentence)
        tokens = [str(tok) for tok in sentence]
        offsets = [tok.idx for tok in sentence]

        return tokens, offsets


class SciSpacySentenceSplitter:
    def __init__(self):
        import spacy

        self.nlp = spacy.load("en_core_sci_sm", disable=["tagger", "ner", "textcat"])

    def __call__(self, text: str):
        doc = self.nlp(text)
        sentences = [str(sent) for sent in doc.sents]
        offsets = [sent.start_char for sent in doc.sents]

        return sentences, offsets


def build_spacy_tokenizer() -> SciSpacyTokenizer:
    try:
        import spacy

        return SciSpacyTokenizer()
    except ImportError:
        raise ValueError(
            "Default tokenizer is scispacy."
            " Install packages 'scispacy' and"
            " 'https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy"
            "/releases/v0.2.4/en_core_sci_sm-0.2.4.tar.gz' via pip"
            " or choose a different tokenizer"
        )


def build_spacy_sentence_splitter() -> SciSpacySentenceSplitter:
    try:
        import spacy

        return SciSpacySentenceSplitter()
    except ImportError:
        raise ValueError(
            "Default sentence splitter is scispacy."
            " Install packages 'scispacy' and"
            "'https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy"
            "/releases/v0.2.4/en_core_sci_sm-0.2.4.tar.gz' via pip"
            " or choose a different sentence splitter"
        )


class HunerDataset(ColumnCorpus, ABC):
    """
    Base class for HUNER Datasets.

    Every subclass has to implement the following methods:
      - `to_internal', which reads the complete data set (incl. train, dev, test) and returns the corpus
        as InternalBioNerDataset
      - `split_url', which returns the base url (i.e. without '.train', '.dev', '.test') to the HUNER split files

    For further information see:
      - Weber et al.: 'HUNER: improving biomedical NER with pretraining'
        https://academic.oup.com/bioinformatics/article-abstract/36/1/295/5523847?redirectedFrom=fulltext
      - HUNER github repository:
        https://github.com/hu-ner/huner
    """

    @staticmethod
    @abstractmethod
    def to_internal(data_folder: Path) -> InternalBioNerDataset:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def split_url() -> str:
        raise NotImplementedError()

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """

        if tokenizer is None:
            tokenizer = build_spacy_tokenizer()

        if sentence_splitter is None:
            sentence_splitter = build_spacy_sentence_splitter()

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            internal_dataset = self.to_internal(data_folder)

            splits_dir = data_folder / "splits"
            os.makedirs(splits_dir, exist_ok=True)

            writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter,
            )

            train_data = self.get_subset(internal_dataset, "train", splits_dir)
            writer.write_to_conll(train_data, train_file)

            dev_data = self.get_subset(internal_dataset, "dev", splits_dir)
            writer.write_to_conll(dev_data, dev_file)

            test_data = self.get_subset(internal_dataset, "test", splits_dir)
            writer.write_to_conll(test_data, test_file)

        super(HunerDataset, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    def get_subset(self, dataset: InternalBioNerDataset, split: str, split_dir: Path):
        split_file = cached_path(f"{self.split_url()}.{split}", split_dir)

        with split_file.open() as f:
            ids = [l.strip() for l in f if l.strip()]
            ids = sorted(id_ for id_ in ids if id_ in dataset.documents)

        return InternalBioNerDataset(
            documents={k: dataset.documents[k] for k in ids},
            entities_per_document={k: dataset.entities_per_document[k] for k in ids},
        )


class HUNER_PROTEIN_BIO_INFER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/bioinfer"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = defaultdict(list)

        data_url = "http://mars.cs.utu.fi/BioInfer/files/BioInfer_corpus_1.1.1.zip"
        data_path = cached_path(data_url, data_dir)
        unzip_file(data_path, data_dir)

        tree = etree.parse(str(data_dir / "BioInfer_corpus_1.1.1.xml"))
        sentence_elems = tree.xpath("//sentence")
        for sentence_id, sentence in enumerate(sentence_elems):
            sentence_id = str(sentence_id)
            token_ids = []
            token_offsets = []
            sentence_text = ""

            all_entity_token_ids = []
            entities = (
                sentence.xpath(".//entity[@type='Individual_protein']")
                + sentence.xpath(".//entity[@type='Gene/protein/RNA']")
                + sentence.xpath(".//entity[@type='Gene']")
                + sentence.xpath(".//entity[@type='DNA_family_or_group']")
            )
            for entity in entities:
                valid_entity = True
                entity_token_ids = set()
                for subtoken in entity.xpath(".//nestedsubtoken"):
                    token_id = ".".join(subtoken.attrib["id"].split(".")[1:3])
                    entity_token_ids.add(token_id)

                if valid_entity:
                    all_entity_token_ids.append(entity_token_ids)

            for token in sentence.xpath(".//token"):
                token_text = "".join(token.xpath(".//subtoken/@text"))
                token_id = ".".join(token.attrib["id"].split(".")[1:])
                token_ids.append(token_id)

                if not sentence_text:
                    token_offsets.append(0)
                    sentence_text = token_text
                else:
                    token_offsets.append(len(sentence_text) + 1)
                    sentence_text += " " + token_text

            documents[sentence_id] = sentence_text

            for entity_token_ids in all_entity_token_ids:
                entity_start = None
                for token_idx, (token_id, token_offset) in enumerate(
                    zip(token_ids, token_offsets)
                ):
                    if token_id in entity_token_ids:
                        if entity_start is None:
                            entity_start = token_offset
                    else:
                        if entity_start is not None:
                            entities_per_document[sentence_id].append(
                                Entity((entity_start, token_offset - 1), "protein")
                            )
                            entity_start = None
        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class JNLPBA(ColumnCorpus):
    """
        Original corpus of the JNLPBA shared task.

        For further information see Kim et al.:
          Introduction to the Bio-Entity Recognition Task at JNLPBA
          https://www.aclweb.org/anthology/W04-1213.pdf
    """

    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and test_file.exists()):
            download_dir = data_folder / "original"
            os.makedirs(download_dir, exist_ok=True)

            train_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Train/Genia4ERtraining.tar.gz"
            train_data_path = cached_path(train_data_url, download_dir)
            unzip_targz_file(train_data_path, download_dir)

            train_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Evaluation/Genia4ERtest.tar.gz"
            train_data_path = cached_path(train_data_url, download_dir)
            unzip_targz_file(train_data_path, download_dir)

            train_file = download_dir / "Genia4ERtask2.iob2"
            shutil.copy(train_file, data_folder / "train.conll")

            test_file = download_dir / "Genia4EReval2.iob2"
            shutil.copy(test_file, data_folder / "test.conll")

        super(JNLPBA, self).__init__(
            data_folder,
            columns,
            tag_to_bioes="ner",
            in_memory=in_memory,
            comment_symbol="#",
        )


class HunerJNLPBA:
    @classmethod
    def download_and_prepare_train(cls, data_folder: Path) -> InternalBioNerDataset:
        train_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Train/Genia4ERtraining.tar.gz"
        train_data_path = cached_path(train_data_url, data_folder)
        unzip_targz_file(train_data_path, data_folder)

        train_input_file = data_folder / "Genia4ERtask2.iob2"
        return cls.read_file(train_input_file)

    @classmethod
    def download_and_prepare_test(cls, data_folder: Path) -> InternalBioNerDataset:
        test_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Evaluation/Genia4ERtest.tar.gz"
        test_data_path = cached_path(test_data_url, data_folder)
        unzip_targz_file(test_data_path, data_folder)

        test_input_file = data_folder / "Genia4EReval2.iob2"
        return cls.read_file(test_input_file)

    @classmethod
    def read_file(cls, input_iob_file: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = defaultdict(list)

        with open(str(input_iob_file), "r") as file_reader:
            document_id = None
            document_text = None

            entities = []
            entity_type = None
            entity_start = 0

            for line in file_reader:
                line = line.strip()
                if line[:3] == "###":
                    if not (document_id is None and document_text is None):
                        documents[document_id] = document_text
                        entities_per_document[document_id] = entities

                    document_id = line.split(":")[-1]
                    document_text = None

                    entities = []
                    entity_type = None
                    entity_start = 0

                    file_reader.__next__()
                    continue

                if line:
                    parts = line.split()
                    token = parts[0].strip()
                    tag = parts[1].strip()

                    if tag.startswith("B-"):
                        if entity_type is not None:
                            entities.append(
                                Entity((entity_start, len(document_text)), entity_type)
                            )

                        entity_start = len(document_text) + 1 if document_text else 0
                        entity_type = tag[2:]

                    elif tag == "O" and entity_type is not None:
                        entities.append(
                            Entity((entity_start, len(document_text)), entity_type)
                        )
                        entity_type = None

                    document_text = (
                        document_text + " " + token if document_text else token
                    )

                else:
                    # Edge case: last token starts a new entity
                    if entity_type is not None:
                        entities.append(
                            Entity((entity_start, len(document_text)), entity_type)
                        )

            # Last document in file
            if not (document_id is None and document_text is None):
                documents[document_id] = document_text
                entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_PROTEIN_JNLPBA(HunerDataset):
    """
        HUNER version of the JNLPBA corpus containing protein annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/genia"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        train_data = HunerJNLPBA.download_and_prepare_train(download_folder)
        train_data = filter_entities(train_data, "protein")

        test_data = HunerJNLPBA.download_and_prepare_test(download_folder)
        test_data = filter_entities(test_data, "protein")

        return merge_datasets([train_data, test_data])


class HUNER_CELL_LINE_JNLPBA(HunerDataset):
    """
        HUNER version of the JNLPBA corpus containing cell line annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/genia"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        train_data = HunerJNLPBA.download_and_prepare_train(download_folder)
        train_data = filter_entities(train_data, "cell_line")

        test_data = HunerJNLPBA.download_and_prepare_test(download_folder)
        test_data = filter_entities(test_data, "cell_line")

        return merge_datasets([train_data, test_data])


class CELL_FINDER(ColumnCorpus):
    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if tokenizer is None:
            tokenizer = build_spacy_tokenizer()

        if sentence_splitter is None:
            sentence_splitter = build_spacy_sentence_splitter()

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        if not (train_file.exists()):
            train_corpus = self.download_and_prepare(data_folder)
            writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter,
            )
            writer.write_to_conll(train_corpus, train_file)
        super(CELL_FINDER, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_and_prepare(cls, data_folder: Path) -> InternalBioNerDataset:
        data_url = "https://www.informatik.hu-berlin.de/de/forschung/gebiete/wbi/resources/cellfinder/cellfinder1_brat.tar.gz"
        data_path = cached_path(data_url, data_folder)
        unzip_targz_file(data_path, data_folder)

        return cls.read_folder(data_folder)

    @classmethod
    def read_folder(cls, data_folder: Path) -> InternalBioNerDataset:
        ann_files = list(data_folder.glob("*.ann"))
        documents = {}
        entities_per_document = defaultdict(list)
        for ann_file in ann_files:
            with ann_file.open() as f_ann, ann_file.with_suffix(".txt").open() as f_txt:
                document_id = ann_file.stem
                for line in f_ann:
                    fields = line.strip().split("\t")
                    if not fields:
                        continue
                    ent_type, char_start, char_end = fields[1].split()
                    entities_per_document[document_id].append(
                        Entity(
                            char_span=(int(char_start), int(char_end)),
                            entity_type=ent_type,
                        )
                    )
                documents[document_id] = f_txt.read()

        return InternalBioNerDataset(
            documents=documents, entities_per_document=dict(entities_per_document)
        )


class HUNER_CELL_LINE_CELL_FINDER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cellfinder_cellline"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        data = CELL_FINDER.download_and_prepare(data_dir)
        data = filter_entities(data, "CellLine")

        return data


class HUNER_SPECIES_CELL_FINDER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cellfinder_species"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        data = CELL_FINDER.download_and_prepare(data_dir)
        data = filter_entities(data, "Species")

        return data


class HUNER_PROTEIN_CELL_FINDER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cellfinder_protein"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        data = CELL_FINDER.download_and_prepare(data_dir)
        data = filter_entities(data, "GeneProtein")

        return data


class MIRNA(ColumnCorpus):
    """
    Original miRNA corpus.

    For further information see Bagewadi et al.:
        Detecting miRNA Mentions and Relations in Biomedical Literature
        https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4602280/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if tokenizer is None:
            tokenizer = build_spacy_tokenizer()

        if sentence_splitter is None:
            sentence_splitter = build_spacy_sentence_splitter()

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and test_file.exists()):
            download_folder = data_folder / "original"
            os.makedirs(str(download_folder), exist_ok=True)

            writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter,
            )

            train_corpus = self.download_and_prepare_train(download_folder)
            writer.write_to_conll(train_corpus, train_file)

            test_corpus = self.download_and_prepare_test(download_folder)
            writer.write_to_conll(test_corpus, test_file)

        super(MIRNA, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_and_prepare_train(cls, data_folder: Path):
        data_url = "https://www.scai.fraunhofer.de/content/dam/scai/de/downloads/bioinformatik/miRNA/miRNA-Train-Corpus.xml"
        data_path = cached_path(data_url, data_folder)

        return cls.parse_file(data_path)

    @classmethod
    def download_and_prepare_test(cls, data_folder: Path):
        data_url = "https://www.scai.fraunhofer.de/content/dam/scai/de/downloads/bioinformatik/miRNA/miRNA-Test-Corpus.xml"
        data_path = cached_path(data_url, data_folder)

        return cls.parse_file(data_path)

    @classmethod
    def parse_file(cls, input_file: Path) -> InternalBioNerDataset:
        tree = etree.parse(str(input_file))

        documents = {}
        entities_per_document = {}

        for document in tree.xpath(".//document"):
            document_id = document.get("id")
            entities = []

            document_text = ""
            for sentence in document.xpath(".//sentence"):
                sentence_offset = len(document_text)
                document_text += sentence.get("text")

                for entity in sentence.xpath(".//entity"):
                    start, end = entity.get("charOffset").split("-")
                    entities.append(
                        Entity(
                            (
                                sentence_offset + int(start),
                                sentence_offset + int(end) + 1,
                            ),
                            entity.get("type"),
                        )
                    )

            documents[document_id] = document_text
            entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_PROTEIN_MIRNA(HunerDataset):
    """
        HUNER version of the miRNA corpus containing protein / gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/miRNA"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        # FIXME: Add entity type normalization!
        train_data = MIRNA.download_and_prepare_train(download_folder)
        train_data = filter_entities(train_data, "Genes/Proteins")

        test_data = MIRNA.download_and_prepare_test(download_folder)
        test_data = filter_entities(test_data, "Genes/Proteins")

        return merge_datasets([train_data, test_data])


class HUNER_SPECIES_MIRNA(HunerDataset):
    """
        HUNER version of the miRNA corpus containing species annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/miRNA"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        # FIXME: Add entity type normalization!
        train_data = MIRNA.download_and_prepare_train(download_folder)
        train_data = filter_entities(train_data, "Species")

        test_data = MIRNA.download_and_prepare_test(download_folder)
        test_data = filter_entities(test_data, "Species")

        return merge_datasets([train_data, test_data])


class HUNER_DISEASE_MIRNA(HunerDataset):
    """
        HUNER version of the miRNA corpus containing disease annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/miRNA"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        # FIXME: Add entity type normalization!
        train_data = MIRNA.download_and_prepare_train(download_folder)
        train_data = filter_entities(train_data, "Diseases")

        test_data = MIRNA.download_and_prepare_test(download_folder)
        test_data = filter_entities(test_data, "Diseases")

        return merge_datasets([train_data, test_data])


class CLL(ColumnCorpus):
    """
    Original CLL corpus.

    For further information, see Kaewphan et al.:
        Cell line name recognition in support of the identification of synthetic lethality in cancer from text
        https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4708107/
    """

    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "ner", 1: "text"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            self.download_corpus(data_folder)
            self.prepare_corpus(data_folder, train_file)

        super(CLL, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_corpus(cls, data_folder: Path):
        data_url = "http://bionlp-www.utu.fi/cell-lines/CLL_corpus.tar.gz"
        data_path = cached_path(data_url, data_folder)
        unzip_targz_file(data_path, data_folder)

    @classmethod
    def prepare_corpus(cls, data_folder: Path, train_file: Path):
        conll_folder = data_folder / "CLL-1.0.2" / "conll"

        sentences = []
        for file in os.listdir(str(conll_folder)):
            if not file.endswith(".conll"):
                continue

            with open(os.path.join(str(conll_folder), file), "r") as reader:
                sentences.append(reader.read())

        with open(str(train_file), "w", encoding="utf8") as writer:
            writer.writelines(sentences)


class HUNER_CELL_LINE_CLL(HunerDataset):
    """
        HUNER version of the miRNA corpus containing protein / gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cll"

    @staticmethod
    def to_internal(data_dir: Path) -> InternalBioNerDataset:
        CLL.download_corpus(data_dir)
        conll_folder = data_dir / "CLL-1.0.2" / "conll"

        documents = {}
        entities_per_document = {}
        for file in os.listdir(str(conll_folder)):
            if not file.endswith(".conll"):
                continue

            document_id = file.replace(".conll", "")

            with open(os.path.join(str(conll_folder), file), "r") as reader:
                document_text = ""
                entities = []

                entity_start = None
                entity_type = None

                for line in reader.readlines():
                    line = line.strip()
                    if line:
                        tag, token = line.split("\t")

                        if tag.startswith("B-"):
                            if entity_type is not None:
                                entities.append(
                                    Entity(
                                        (entity_start, len(document_text)), entity_type
                                    )
                                )

                            entity_start = (
                                len(document_text) + 1 if document_text else 0
                            )
                            entity_type = tag[2:]

                        elif tag == "O" and entity_type is not None:
                            entities.append(
                                Entity((entity_start, len(document_text)), entity_type,)
                            )
                            entity_type = None

                        document_text = (
                            document_text + " " + token if document_text else token
                        )
                    else:
                        # Edge case: last token starts a new entity
                        if entity_type is not None:
                            entities.append(
                                Entity((entity_start, len(document_text)), entity_type)
                            )

                documents[document_id] = document_text
                entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )
