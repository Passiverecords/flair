import logging
import os
import pickle
import re
import stat
import string
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections import defaultdict
from enum import Enum, auto
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

import faiss
import numpy as np
import torch
from huggingface_hub import cached_download, hf_hub_url
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm

import flair
from flair.data import EntityLinkingLabel, Label, Sentence
from flair.datasets import (
    NEL_CTD_CHEMICAL_DICT,
    NEL_CTD_DISEASE_DICT,
    NEL_NCBI_HUMAN_GENE_DICT,
    NEL_NCBI_TAXONOMY_DICT,
)
from flair.datasets.biomedical import PreprocessedBioNelDictionary
from flair.embeddings import TransformerDocumentEmbeddings
from flair.file_utils import cached_path

logger = logging.getLogger("flair")

BIOMEDICAL_NEL_DICTIONARIES = {
    "ctd-disease": NEL_CTD_DISEASE_DICT,
    "ctd-chemical": NEL_CTD_CHEMICAL_DICT,
    "ncbi-gene": NEL_NCBI_HUMAN_GENE_DICT,
    "ncbi-taxonomy": NEL_NCBI_TAXONOMY_DICT,
}

PRETRAINED_MODELS = [
    "cambridgeltl/SapBERT-from-PubMedBERT-fulltext",
]

# Dense + sparse retrieval
PRETRAINED_HYBRID_MODELS = [
    "biosyn-sapbert-bc5cdr-disease",
    "biosyn-sapbert-ncbi-disease",
    "biosyn-sapbert-bc5cdr-chemical",
    "biosyn-biobert-bc5cdr-disease",
    "biosyn-biobert-ncbi-disease",
    "biosyn-biobert-bc5cdr-chemical",
    "biosyn-biobert-bc2gn",
    "biosyn-sapbert-bc2gn",
]

PRETRAINED_MODELS = PRETRAINED_HYBRID_MODELS + PRETRAINED_MODELS

# just in case we add: fuzzy search, Levenstein, ...
STRING_MATCHING_MODELS = ["exact-string-match"]

MODELS = PRETRAINED_MODELS + STRING_MATCHING_MODELS

ENTITY_TYPES = ["disease", "chemical", "gene", "species"]

ENTITY_TYPE_TO_HYBRID_MODEL = {
    "disease": "dmis-lab/biosyn-sapbert-bc5cdr-disease",
    "chemical": "dmis-lab/biosyn-sapbert-bc5cdr-chemical",
    "gene": "dmis-lab/biosyn-sapbert-bc2gn",
}

# for now we always fall back to SapBERT,
# but we should train our own models at some point
ENTITY_TYPE_TO_DENSE_MODEL = {
    entity_type: "cambridgeltl/SapBERT-from-PubMedBERT-fulltext" for entity_type in ENTITY_TYPES
}

DEFAULT_SPARSE_WEIGHT = 0.5

ENTITY_TYPE_TO_NEL_DICTIONARY = {
    "gene": "ncbi-gene",
    "species": "ncbi-taxonomy",
    "disease": "ctd-disease",
    "chemical": "ctd-chemical",
}

MODEL_NAME_TO_NEL_DICTIONARY = {
    "biosyn-sapbert-bc5cdr-disease": "ctd-disease",
    "biosyn-sapbert-ncbi-disease": "ctd-disease",
    "biosyn-sapbert-bc5cdr-chemical": "ctd-chemical",
    "biosyn-biobert-bc5cdr-disease": "ctd-chemical",
    "biosyn-biobert-ncbi-disease": "ctd-disease",
    "biosyn-biobert-bc5cdr-chemical": "ctd-chemical",
    "biosyn-biobert-bc2gn": "ncbi-gene",
    "biosyn-sapbert-bc2gn": "ncbi-gene",
}


class SimilarityMetric(Enum):
    """
    Available similarity metrics
    """

    INNER_PRODUCT = faiss.METRIC_INNER_PRODUCT
    # L2 = faiss.METRIC_L2
    COSINE = auto()


class BioNelPreprocessor(ABC):
    """
    A entity pre-processor is used to transform / clean an entity mention (recognized by
    an entity recognition model in the original text). This may include removing certain characters
    (e.g. punctuation) or converting characters (e.g. HTML-encoded characters) as well as
    (more sophisticated) domain-specific procedures.

    This class provides the basic interface for such transformations and should be extended by
    subclasses that implement concrete transformations.
    """

    @abstractmethod
    def process_mention(self, entity_mention: Label, sentence: Sentence) -> str:
        """
        Processes the given entity mention and applies the transformation procedure to it.

        :param entity_mention: entity mention under investigation
        :param sentence: sentence in which the entity mentioned occurred
        :result: Cleaned / transformed string representation of the given entity mention
        """

    @abstractmethod
    def process_entry(self, entity_name: str) -> str:
        """
        Processes the given entity name (originating from a knowledge base / ontology) and
        applies the transformation procedure to it.

        :param entity_name: entity mention given as DataPoint
        :result: Cleaned / transformed string representation of the given entity mention
        """
        raise NotImplementedError()

    @abstractmethod
    def initialize(self, sentences: List[Sentence]):
        """
        Initializes the pre-processor for a batch of sentences, which is may be necessary for
        more sophisticated transformations.

        :param sentences: List of sentences that will be processed.
        """


class BasicBioNelPreprocessor(BioNelPreprocessor):
    """
    Basic implementation of MentionPreprocessor, which supports lowercasing, typo correction
     and removing of punctuation characters.

    Implementation is adapted from:
        Sung et al. 2020, Biomedical Entity Representations with Synonym Marginalization
        https://github.com/dmis-lab/BioSyn/blob/master/src/biosyn/preprocesser.py#L5
    """

    def __init__(
        self, lowercase: bool = True, remove_punctuation: bool = True, punctuation_symbols: str = string.punctuation
    ) -> None:
        """
        Initializes the mention preprocessor.

        :param lowercase: Indicates whether to perform lowercasing or not (True by default)
        :param remove_punctuation: Indicates whether to perform removal punctuations symbols (True by default)
        :param punctuation_symbols: String containing all punctuation symbols that should be removed
            (default is given by string.punctuation)
        """
        self.lowercase = lowercase
        self.remove_punctuation = remove_punctuation
        self.rmv_puncts_regex = re.compile(r"[\s{}]+".format(re.escape(punctuation_symbols)))

    def initialize(self, sentences):
        pass

    def process_entry(self, entity_name: str) -> str:
        if self.lowercase:
            entity_name = entity_name.lower()

        if self.remove_punctuation:
            entity_name = self.rmv_puncts_regex.split(entity_name)
            entity_name = " ".join(entity_name).strip()

        return entity_name.strip()

    def process_mention(self, entity_mention: Label, sentence: Sentence) -> str:
        return self.process_entry(entity_mention.data_point.text)


class Ab3PPreprocessor(BioNelPreprocessor):
    """
    Implementation of MentionPreprocessor which utilizes Ab3P, an (biomedical)abbreviation definition detector,
    given in:
        https://github.com/ncbi-nlp/Ab3P

    Ab3P applies a set of rules reflecting simple patterns such as Alpha Beta (AB) as well as more involved cases.
    The algorithm is described in detail in the following paper:

        Abbreviation definition identification based on automatic precision estimates.
        Sohn S, Comeau DC, Kim W, Wilbur WJ. BMC Bioinformatics. 2008 Sep 25;9:402.
        PubMed ID: 18817555
    """

    def __init__(self, ab3p_path: Path, word_data_dir: Path, preprocessor: Optional[BioNelPreprocessor] = None) -> None:
        """
        Creates the mention pre-processor

        :param ab3p_path: Path to the folder containing the Ab3P implementation
        :param word_data_dir: Path to the word data directory
        :param preprocessor: Entity mention text preprocessor that is used before trying to link
            the mention text to an abbreviation.
        """
        self.ab3p_path = ab3p_path
        self.word_data_dir = word_data_dir
        self.preprocessor = preprocessor
        self.abbreviation_dict = {}

    def initialize(self, sentences: List[Sentence]) -> None:
        self.abbreviation_dict = self._build_abbreviation_dict(sentences)

    def process_mention(self, entity_mention: Label, sentence: Sentence) -> str:
        sentence_text = sentence.to_tokenized_string().strip()
        tokens = [token.text for token in entity_mention.data_point.tokens]

        parsed_tokens = []
        for token in tokens:
            if self.preprocessor is not None:
                token = self.preprocessor.process_entry(token)

            if sentence_text in self.abbreviation_dict:
                if token.lower() in self.abbreviation_dict[sentence_text]:
                    parsed_tokens.append(self.abbreviation_dict[sentence_text][token.lower()])
                    continue

            if len(token) != 0:
                parsed_tokens.append(token)

        return " ".join(parsed_tokens)

    def process_entry(self, entity_name: str) -> str:
        # Ab3P works on sentence-level and not on a single entity mention / name
        # - so we just apply the wrapped text pre-processing here (if configured)
        if self.preprocessor is not None:
            return self.preprocessor.process_entry(entity_name)

        return entity_name

    @classmethod
    def load(cls, ab3p_path: Path = None, preprocessor: Optional[BioNelPreprocessor] = None):
        data_dir = flair.cache_root / "ab3p"
        if not data_dir.exists():
            data_dir.mkdir(parents=True)

        word_data_dir = data_dir / "word_data"
        if not word_data_dir.exists():
            word_data_dir.mkdir()

        if ab3p_path is None:
            ab3p_path = cls.download_ab3p(data_dir, word_data_dir)

        return cls(ab3p_path, word_data_dir, preprocessor)

    @classmethod
    def download_ab3p(cls, data_dir: Path, word_data_dir: Path) -> Path:
        """
        Downloads the Ab3P tool and all necessary data files.
        """

        # Download word data for Ab3P if not already downloaded
        ab3p_url = "https://raw.githubusercontent.com/dmis-lab/BioSyn/master/Ab3P/WordData/"

        ab3p_files = [
            "Ab3P_prec.dat",
            "Lf1chSf",
            "SingTermFreq.dat",
            "cshset_wrdset3.ad",
            "cshset_wrdset3.ct",
            "cshset_wrdset3.ha",
            "cshset_wrdset3.nm",
            "cshset_wrdset3.str",
            "hshset_Lf1chSf.ad",
            "hshset_Lf1chSf.ha",
            "hshset_Lf1chSf.nm",
            "hshset_Lf1chSf.str",
            "hshset_stop.ad",
            "hshset_stop.ha",
            "hshset_stop.nm",
            "hshset_stop.str",
            "stop",
        ]
        for file in ab3p_files:
            cached_path(ab3p_url + file, word_data_dir)

        # Download Ab3P executable
        ab3p_path = cached_path("https://github.com/dmis-lab/BioSyn/raw/master/Ab3P/identify_abbr", data_dir)

        ab3p_path.chmod(ab3p_path.stat().st_mode | stat.S_IXUSR)
        return ab3p_path

    def _build_abbreviation_dict(self, sentences: List[flair.data.Sentence]) -> Dict[str, Dict[str, str]]:
        """
        Processes the given sentences with the Ab3P tool. The function returns a (nested) dictionary
        containing the abbreviations found for each sentence, e.g.:

        {
            "Respiratory syncytial viruses ( RSV ) are a subgroup of the paramyxoviruses.":
                {"RSV": "Respiratory syncytial viruses"},
            "Rous sarcoma virus ( RSV ) is a retrovirus.":
                {"RSV": "Rous sarcoma virus"}
        }
        """
        abbreviation_dict = defaultdict(dict)

        # Create a temp file which holds the sentences we want to process with ab3p
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as temp_file:
            for sentence in sentences:
                temp_file.write(sentence.to_tokenized_string() + "\n")
            temp_file.flush()

            # Temporarily create path file in the current working directory for Ab3P
            with open(os.path.join(os.getcwd(), "path_Ab3P"), "w") as path_file:
                path_file.write(str(self.word_data_dir) + "/\n")

            # Run ab3p with the temp file containing the dataset
            # https://pylint.pycqa.org/en/latest/user_guide/messages/warning/subprocess-run-check.html
            try:
                result = subprocess.run(
                    [self.ab3p_path, temp_file.name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            except subprocess.CalledProcessError:
                logger.error(
                    """The abbreviation resolver Ab3P could not be run on your system. To ensure maximum accuracy, please
                install Ab3P yourself. See https://github.com/ncbi-nlp/Ab3P"""
                )
            else:
                line = result.stdout.decode("utf-8")
                if "Path file for type cshset does not exist!" in line:
                    logger.error(
                        "Error when using Ab3P for abbreviation resolution. A file named path_Ab3p needs to exist in your current directory containing the path to the WordData directory for Ab3P to work!"
                    )
                elif "Cannot open" in line:
                    logger.error(
                        "Error when using Ab3P for abbreviation resolution. Could not open the WordData directory for Ab3P!"
                    )
                elif "failed to open" in line:
                    logger.error(
                        "Error when using Ab3P for abbreviation resolution. Could not open the WordData directory for Ab3P!"
                    )

                lines = line.split("\n")
                cur_sentence = None
                for line in lines:
                    if len(line.split("|")) == 3:
                        if cur_sentence is None:
                            continue

                        sf, lf, _ = line.split("|")
                        sf = sf.strip().lower()
                        lf = lf.strip().lower()
                        abbreviation_dict[cur_sentence][sf] = lf

                    elif len(line.strip()) > 0:
                        cur_sentence = line
                    else:
                        cur_sentence = None

            finally:
                # remove the path file
                os.remove(os.path.join(os.getcwd(), "path_Ab3P"))

        return abbreviation_dict


class BigramTfIDFVectorizer:
    """
    Helper class to encode a list of entity mentions or dictionary entries into a sparse tensor.

    Implementation adapted from:
        Sung et al.: Biomedical Entity Representations with Synonym Marginalization, 2020
        https://github.com/dmis-lab/BioSyn/tree/master/src/biosyn/sparse_encoder.py#L8
    """

    def __init__(self) -> None:
        self.encoder = TfidfVectorizer(analyzer="char", ngram_range=(1, 2))

    def fit(self, names: List[str]):
        """
        Fit vectorizer
        """
        self.encoder.fit(names)
        return self

    def transform(self, names: List[str]) -> torch.Tensor:
        """
        Convert string names to sparse vectors
        """
        vec = self.encoder.transform(names).toarray()
        vec = torch.FloatTensor(vec)
        return vec

    def __call__(self, mentions: List[str]) -> torch.Tensor:
        """
        Short for `transform`
        """
        return self.transform(mentions)

    def save_encoder(self, path: Path) -> None:
        with path.open("wb") as fout:
            pickle.dump(self.encoder, fout)
            logger.info("Sparse encoder saved in %s", path)

    @classmethod
    def load(cls, path: Path) -> "BigramTfIDFVectorizer":
        """
        Instantiate from path
        """
        newVectorizer = cls()
        with open(path, "rb") as fin:
            newVectorizer.encoder = pickle.load(fin)
            logger.info("Sparse encoder loaded from %s", path)

        return newVectorizer


class BioNelDictionary:
    """
    A class used to load dictionary data from a custom dictionary file.
    Every line in the file must be formatted as follows:
    concept_unique_id||concept_name
    with one line per concept name. Multiple synonyms for the same concept should
    be in separate lines with the same concept_unique_id.

    Slightly modifed from Sung et al. 2020
    Biomedical Entity Representations with Synonym Marginalization
    https://github.com/dmis-lab/BioSyn/blob/master/src/biosyn/data_loader.py#L89
    """

    def __init__(self, reader):
        self.reader = reader

    @classmethod
    def load(cls, dictionary_name_or_path: Union[Path, str]) -> "BioNelDictionary":
        """
        Load dictionary: either pre-definded or from path
        """

        if isinstance(dictionary_name_or_path, str):
            if (
                dictionary_name_or_path not in ENTITY_TYPE_TO_NEL_DICTIONARY
                and dictionary_name_or_path not in BIOMEDICAL_NEL_DICTIONARIES
            ):
                raise ValueError(
                    f"""Unkwnon dictionary `{dictionary_name_or_path}`,
                    Available dictionaries are: {tuple(BIOMEDICAL_NEL_DICTIONARIES)} \n
                    If you want to pass a local path please use the `Path` class, i.e. `model_name_or_path=Path(my_path)`"""
                )

            dictionary_name_or_path = ENTITY_TYPE_TO_NEL_DICTIONARY.get(
                dictionary_name_or_path, dictionary_name_or_path
            )

            reader = BIOMEDICAL_NEL_DICTIONARIES[dictionary_name_or_path]()

        else:
            # use custom dictionary file
            reader = PreprocessedBioNelDictionary(path=dictionary_name_or_path)

        return cls(reader=reader)

    def get_database_names(self) -> List[str]:
        """
        List all database names covered by dictionary, e.g. MESH, OMIM
        """

        return self.reader.get_database_names()

    def stream(self) -> Iterator[Tuple[str, str]]:
        """
        Stream preprocessed dictionary
        """

        for entry in self.reader.stream():
            yield entry


class EntityRetriever(ABC):
    """
    An entity retriever model is used to find the top-k entities / concepts of a knowledge base /
    dictionary for a given entity mention in text.
    """

    @abstractmethod
    def search(self, entity_mentions: List[str], top_k: int) -> List[Tuple[str, str, float]]:
        """
        Returns the top-k entity / concept identifiers for the given entity mention.

        :param entity_mentions: Entity mention text under investigation
        :param top_k: Number of (best-matching) entities from the knowledge base to return
        :result: List of tuples highlighting the top-k entities. Each tuple has the following
            structure (entity / concept name, concept ids, score).
        """


class ExactStringMatchingRetriever(EntityRetriever):
    """
    Implementation of an entity retriever model which uses exact string matching to
    find the entity / concept identifier for a given entity mention.
    """

    def __init__(self, dictionary: BioNelDictionary):
        # Build index which maps concept / entity names to concept / entity ids
        self.name_to_id_index = dict(dictionary.data)

    @classmethod
    def load(cls, dictionary_name_or_path: str) -> "ExactStringMatchingRetrieverModel":
        """
        Compatibility function
        """
        # Load dictionary
        return cls(BioNelDictionary.load(dictionary_name_or_path))

    def search(self, entity_mentions: List[str], top_k: int) -> List[Tuple[str, str, float]]:
        """
        Returns the top-k entity / concept identifiers for the given entity mention. Note that
        the model either returns the entity with an identical name in the knowledge base / dictionary
        or none.

        :param entity_mention: Entity mention under investigation
        :param top_k: Number of (best-matching) entities from the knowledge base to return
        :result: List of tuples highlighting the top-k entities. Each tuple has the following
            structure (entity / concept name, concept ids, score).
        """

        return [(em, self.name_to_id_index.get(em), 1.0) for em in entity_mentions]


class BiEncoderEntityRetriever(EntityRetriever):
    """
    Implementation of EntityRetrieverModel which uses dense (transformer-based) embeddings and (optionally)
    sparse character-based representations, for normalizing an entity mention to specific identifiers
    in a knowledge base / dictionary.

    To this end, the model embeds the entity mention text and all concept names from the knowledge
    base and outputs the k best-matching concepts based on embedding similarity.
    """

    def __init__(
        self,
        model_name_or_path: Union[str, Path],
        dictionary_name_or_path: str,
        hybrid_search: bool = False,
        max_length: int = 25,
        index_batch_size: int = 1024,
        preprocessor: BioNelPreprocessor = Ab3PPreprocessor.load(preprocessor=BasicBioNelPreprocessor()),
        similarity_metric: SimilarityMetric = SimilarityMetric.COSINE,
        sparse_weight: Optional[float] = None,
    ):
        """
        Initializes the BiEncoderEntityRetrieverModel.

        :param model_name_or_path: Name of or path to the transformer model to be used.
        :param dictionary_name_or_path: Name of or path to the transformer model to be used.
        :param hybrid_search: Indicates whether to use sparse embeddings or not
        :param use_cosine: Indicates whether to use cosine similarity (instead of inner product)
        :param max_length: Maximal number of tokens used for embedding an entity mention / concept name
        :param index_batch_size: Batch size used during embedding of the dictionary and top-k prediction
        :param similarity_metric: which metric to use to compute similarity
        :param sparse_weight: default sparse weight
        :param preprocessor: Preprocessing strategy to clean and transform entity / concept names from the knowledge base
        """
        self.preprocessor = preprocessor
        self.similarity_metric = similarity_metric
        self.max_length = max_length
        self.index_batch_size = index_batch_size
        self.hybrid_search = hybrid_search
        self.sparse_weight = sparse_weight

        # Load dense encoder
        self.dense_encoder = TransformerDocumentEmbeddings(model=model_name_or_path, is_token_embedding=False)

        # Load dictionary
        self.dictionary = BioNelDictionary.load(dictionary_name_or_path)

        self.embeddings = self._load_emebddings(
            model_name_or_path=model_name_or_path,
            dictionary_name_or_path=dictionary_name_or_path,
            batch_size=self.index_batch_size,
        )

        # Build dense embedding index using faiss
        dimension = self.embeddings["dense"].shape[1]
        self.dense_index = faiss.IndexFlatIP(dimension)
        self.dense_index.add(self.embeddings["dense"])

        self.sparse_encoder: Optional[BigramTfIDFVectorizer] = None
        if self.hybrid_search:
            self._set_sparse_encoder(model_name_or_path=model_name_or_path)

    def _set_sparse_encoder(self, model_name_or_path: Union[str, Path]) -> BigramTfIDFVectorizer:

        sparse_encoder_path = os.path.join(model_name_or_path, "sparse_encoder.pk")
        sparse_weight_path = os.path.join(model_name_or_path, "sparse_weight.pt")

        # check file exists
        if not os.path.isfile(sparse_encoder_path):
            # download from huggingface hub and cache it
            sparse_encoder_url = hf_hub_url(model_name_or_path, filename="sparse_encoder.pk")
            sparse_encoder_path = cached_download(
                url=sparse_encoder_url,
                cache_dir=flair.cache_root / "models" / model_name_or_path,
            )

        self.sparse_encoder = BigramTfIDFVectorizer.load(path=sparse_encoder_path)

        # check file exists
        if not os.path.isfile(sparse_weight_path):
            # download from huggingface hub and cache it
            sparse_weight_url = hf_hub_url(model_name_or_path, filename="sparse_weight.pt")
            sparse_weight_path = cached_download(
                url=sparse_weight_url,
                cache_dir=flair.cache_root / "models" / model_name_or_path,
            )

        self.sparse_weight = torch.load(sparse_weight_path, map_location="cpu").item()

        return self.sparse_weight

    def embed_sparse(self, inputs: np.ndarray) -> np.ndarray:
        """
        Embeds the given numpy array of entity names, either originating from the knowledge base
        or recognized in a text, into sparse representations.

        :param entity_names: An array of entity / concept names
        :returns sparse_embeds np.array: Numpy array containing the sparse embeddings
        """
        sparse_embeds = self.sparse_encoder(inputs)
        sparse_embeds = sparse_embeds.numpy()

        if self.similarity_metric == SimilarityMetric.COS:
            faiss.normalize_L2(sparse_embeds)

        return sparse_embeds

    def embed_dense(self, inputs: np.ndarray, batch_size: int = 1024, show_progress: bool = False) -> np.ndarray:
        """
        Embeds the given numpy array of entity / concept names, either originating from the
        knowledge base or recognized in a text, into dense representations using a
        TransformerDocumentEmbedding model.

        :param names: Numpy array of entity / concept names
        :param batch_size: Batch size used while embedding the name
        :param show_progress: bool to toggle progress bar
        :return: Numpy array containing the dense embeddings of the names
        """
        self.dense_encoder.eval()  # prevent dropout

        dense_embeds = []

        with torch.no_grad():
            if show_progress:
                iterations = tqdm(
                    range(0, len(inputs), batch_size),
                    desc="Calculating dense embeddings for dictionary",
                )
            else:
                iterations = range(0, len(inputs), batch_size)

            for start in iterations:
                # Create batch
                end = min(start + batch_size, len(inputs))
                batch = [Sentence(name) for name in inputs[start:end]]

                # embed batch
                self.dense_encoder.embed(batch)

                dense_embeds += [name.embedding.cpu().detach().numpy() for name in batch]

                if flair.device.type == "cuda":
                    torch.cuda.empty_cache()

        dense_embeds = np.array(dense_embeds)

        return dense_embeds

    def _load_emebddings(self, model_name_or_path: str, dictionary_name_or_path: str, batch_size: int):
        """
        Computes the embeddings for the given knowledge base / dictionary.
        """

        # Check for embedded dictionary in cache
        dictionary_name = os.path.splitext(os.path.basename(dictionary_name_or_path))[0]
        file_name = f"bio_nen_{model_name_or_path.split('/')[-1]}_{dictionary_name}"

        cache_folder = flair.cache_root / "datasets"

        embeddings_cache_file = cache_folder / f"{file_name}.pk"

        # If exists, load the cached dictionary indices
        if embeddings_cache_file.exists():

            with embeddings_cache_file.open("rb") as fp:
                logger.info("Load cached emebddings from  %s", embeddings_cache_file)
                embeddings = pickle.load(fp)

        else:

            cache_folder.mkdir(parents=True, exist_ok=True)

            names = self.dictionary.to_names(preprocessor=self.preprocessor)

            # Compute dense embeddings (if necessary)
            dense_embeddings = self.embed_dense(inputs=names, batch_size=batch_size, show_progress=True)
            sparse_embeddings = self.embed_sparse(inputs=names) if self.hybrid_search else None

            # Store the pre-computed index on disk for later re-use
            embeddings = {
                "dense": dense_embeddings,
                "sparse": sparse_embeddings,
            }

            logger.info("Caching preprocessed dictionary into %s", cache_folder)
            with embeddings_cache_file.open("wb") as fp:
                pickle.dump(embeddings, fp)

        if self.similarity_metric == SimilarityMetric.COS:
            faiss.normalize_L2(embeddings["dense"])

        return embeddings

    def search_sparse(
        self,
        entity_mentions: List[str],
        top_k: int = 1,
        normalise: bool = False,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Returns top-k indexes (in descending order) for the given entity mentions resp. mention
        embeddings.

        :param score_matrix: 2d numpy array of scores
        :param top_k: number of candidates to retrieve
        :return res: d numpy array of ids [# of query , # of dict]
        :return scores: numpy array of top scores
        """

        mention_embeddings = self.sparse_encoder(entity_mentions)

        if self.similarity_metric == SimilarityMetric.COSINE:
            score_matrix = cosine_similarity(mention_embeddings, self.embeddings["sparse"])
        else:
            score_matrix = np.matmul(mention_embeddings, self.embeddings["sparse"].T)

        if normalise:
            score_matrix = (score_matrix - score_matrix.min()) / (score_matrix.max() - score_matrix.min())

        def indexing_2d(arr, cols):
            rows = np.repeat(np.arange(0, cols.shape[0])[:, np.newaxis], cols.shape[1], axis=1)
            return arr[rows, cols]

        # Get topk indexes without sorting
        topk_idxs = np.argpartition(score_matrix, -top_k)[:, -top_k:]

        # Get topk indexes with sorting
        topk_score_matrix = indexing_2d(score_matrix, topk_idxs)
        topk_argidxs = np.argsort(-topk_score_matrix)
        topk_idxs = indexing_2d(topk_idxs, topk_argidxs)
        topk_scores = indexing_2d(score_matrix, topk_idxs)

        return (topk_idxs, topk_scores)

    def search_dense(self, entity_mentions: List[str], top_k: int = 1) -> Tuple[np.ndarray, np.ndarray]:
        """
        Dense search via FAISS index
        """

        # Compute dense embedding for the given entity mention
        mention_dense_embeds = self.embed_dense(inputs=np.array(entity_mentions), batch_size=self.index_batch_size)

        # Get candidates from dense embeddings
        dists, ids = self.dense_index.search(x=mention_dense_embeds, top_k=top_k)

        return dists, ids

    def search(self, entity_mentions: List[str], top_k: int) -> List[Tuple[str, str, float]]:
        """
        Returns the top-k entities for a given entity mention.

        :param entity_mentions: Entity mentions (search queries)
        :param top_k: Number of (best-matching) entities from the knowledge base to return
        :result: List of tuples w/ the top-k entities: (concept name, concept ids, score).
        """

        # dense

        # # If using sparse embeds: calculate hybrid scores with dense and sparse embeds
        # if self.use_sparse_embeds:
        #     # Get sparse embeddings for the entity mention
        #     mention_sparse_embeds = self.embed_sparse(entity_names=np.array([entity_mention]))

        #     # Get candidates from sparse embeddings
        #     sparse_ids, sparse_distances = self.search_sparse(
        #         mention_embeddings=mention_sparse_embeds,
        #         dict_concept_embeddings=self.dict_sparse_embeddings,
        #         top_k=top_k + self.top_k_extra_sparse,
        #     )

        #     # Combine dense and sparse scores
        #     sparse_weight = self.sparse_weight
        #     hybrid_ids = []
        #     hybrid_scores = []

        #     # For every embedded mention
        #     for (
        #         top_dense_ids,
        #         top_dense_scores,
        #         top_sparse_ids,
        #         top_sparse_distances,
        #     ) in zip(dense_ids, dense_scores, sparse_ids, sparse_distances):
        #         ids = top_dense_ids
        #         distances = top_dense_scores

        #         for sparse_id, sparse_distance in zip(top_sparse_ids, top_sparse_distances):
        #             if sparse_id not in ids:
        #                 ids = np.append(ids, sparse_id)
        #                 distances = np.append(distances, sparse_weight * sparse_distance)
        #             else:
        #                 index = np.where(ids == sparse_id)[0][0]
        #                 distances[index] = (sparse_weight * sparse_distance) + distances[index]

        #         sorted_indizes = np.argsort(-distances)
        #         ids = ids[sorted_indizes][:top_k]
        #         distances = distances[sorted_indizes][:top_k]
        #         hybrid_ids.append(ids.tolist())
        #         hybrid_scores.append(distances.tolist())

        # else:
        #     # Use only dense embedding results
        #     hybrid_ids = dense_ids
        #     hybrid_scores = dense_scores

        # return [
        #     tuple(self.dictionary[entity_index].reshape(1, -1)[0]) + (score[0],)
        #     for entity_index, score in zip(hybrid_ids, hybrid_scores)
        # ]


class BiomedicalEntityLinker:
    """
    Entity linking model which expects text/sentences with annotated entity mentions and predicts
    entity / concept to these mentions according to a knowledge base / dictionary.
    """

    def __init__(self, retriever_model: EntityRetriever, mention_preprocessor: BioNelPreprocessor):
        self.preprocessor = mention_preprocessor
        self.retriever_model = retriever_model

    def predict(
        self,
        sentences: Union[List[Sentence], Sentence],
        input_entity_annotation_layer: str = None,
        top_k: int = 1,
    ) -> None:
        """
        Predicts the best matching top-k entity / concept identifiers of all named entites annotated
        with tag input_entity_annotation_layer.

        :param sentences: One or more sentences to run the prediction on
        :param input_entity_annotation_layer: Entity type to run the prediction on
        :param top_k: Number of best-matching entity / concept identifiers which should be predicted
            per entity mention
        """
        # make sure sentences is a list of sentences
        if not isinstance(sentences, list):
            sentences = [sentences]

        if self.preprocessor is not None:
            self.preprocessor.initialize(sentences)

        # Build label name
        label_name = input_entity_annotation_layer + "_nen" if (input_entity_annotation_layer is not None) else "nen"

        # For every sentence ..
        for sentence in sentences:
            # ... process every mentioned entity
            for entity in sentence.get_labels(input_entity_annotation_layer):
                # Pre-process entity mention (if necessary)
                mention_text = (
                    self.preprocessor.process_mention(entity, sentence)
                    if self.preprocessor is not None
                    else entity.data_point.text
                )

                # Retrieve top-k concept / entity candidates
                predictions = self.retriever_model.search(mention_text, top_k)

                # Add a label annotation for each candidate
                for prediction in predictions:
                    # if concept identifier is made up of multiple ids, separated by '|'
                    # separate it into cui and additional_labels
                    cui = prediction[1]
                    if "|" in cui:
                        labels = cui.split("|")
                        cui = labels[0]
                        additional_labels = labels[1:]
                    else:
                        additional_labels = None

                    # determine database:
                    if ":" in cui:
                        cui_parts = cui.split(":")
                        database = ":".join(cui_parts[0:-1])
                        cui = cui_parts[-1]
                    else:
                        database = None

                    sentence.add_label(
                        typename=label_name,
                        value_or_label=EntityLinkingLabel(
                            data_point=entity.data_point,
                            concept_id=cui,
                            concept_name=prediction[0],
                            additional_ids=additional_labels,
                            database=database,
                            score=prediction[2],
                        ),
                    )

    @classmethod
    def load(
        cls,
        model_name_or_path: Union[str, Path],
        dictionary_name_or_path: Union[str, Path] = None,
        hybrid_search: bool = True,
        max_length: int = 25,
        index_batch_size: int = 1024,
        similarity_metric: SimilarityMetric = SimilarityMetric.COSINE,
        preprocessor: BioNelPreprocessor = Ab3PPreprocessor.load(preprocessor=BasicBioNelPreprocessor()),
        default_sparse_encoder: bool = False,
        sparse_weight: float = DEFAULT_SPARSE_WEIGHT,
    ):
        """
        Loads a model for biomedical named entity normalization.
        See __init__ method for detailed docstring on arguments
        """
        dictionary_path = dictionary_name_or_path
        if dictionary_name_or_path is None or isinstance(dictionary_name_or_path, str):
            dictionary_path = cls.__get_dictionary_path(dictionary_name_or_path, model_name_or_path)

        retriever_model = None
        if isinstance(model_name_or_path, str):
            if model_name_or_path == "exact-string-match":
                retriever_model = ExactStringMatchingRetriever.load(dictionary_path)
            else:
                model_path = cls.__get_model_path(
                    model_name_or_path=model_name_or_path,
                    hybrid_search=hybrid_search,
                    default_sparse_encoder=default_sparse_encoder,
                )
                retriever_model = BiEncoderEntityRetriever(
                    model_name_or_path=model_path,
                    dictionary_name_or_path=dictionary_path,
                    hybrid_search=hybrid_search,
                    similarity_metric=similarity_metric,
                    max_length=max_length,
                    index_batch_size=index_batch_size,
                    sparse_weight=sparse_weight,
                    preprocessor=preprocessor,
                )

        return cls(retriever_model=retriever_model, mention_preprocessor=preprocessor)

    @staticmethod
    def __get_model_path(
        model_name_or_path: Union[str, Path], hybrid_search: bool = False, default_sparse_encoder: bool = False
    ) -> str:
        """
        Try to figure out what model the user wants
        """

        if isinstance(model_name_or_path, str):

            model_name_or_path = model_name_or_path.lower()

            if model_name_or_path not in MODELS and model_name_or_path not in ENTITY_TYPES:
                raise ValueError(
                    f"""Unknown model `{model_name_or_path}`! \n
                        Available entity types are: {ENTITY_TYPES} \n
                        If you want to pass a local path please use the `Path` class, i.e. `model_name_or_path=Path(my_path)`"""
                )

            if hybrid_search:

                # load model by entity_type
                if model_name_or_path in ENTITY_TYPES:
                    # check if we have a hybrid pre-trained model
                    if model_name_or_path in ENTITY_TYPE_TO_HYBRID_MODEL:
                        model_name_or_path = ENTITY_TYPE_TO_HYBRID_MODEL[model_name_or_path]
                    else:
                        # check if user really wants to use hybrid search anyway
                        if not default_sparse_encoder:
                            raise ValueError(
                                f"""Model for entity type `{model_name_or_path}` was not trained for hybrid search! \n
                                If you want to proceed anyway please pass `default_sparse_encoder=True`:
                                we will fit a sparse encoder for you. The default value of `sparse_weight` is `{DEFAULT_SPARSE_WEIGHT}`.
                                """
                            )
                        model_name_or_path = ENTITY_TYPE_TO_DENSE_MODEL[model_name_or_path]
                else:
                    if model_name_or_path not in PRETRAINED_HYBRID_MODELS and not default_sparse_encoder:
                        raise ValueError(
                            f"""Model `{model_name_or_path}` was not trained for hybrid search! \n
                            If you want to proceed anyway please pass `default_sparse_encoder=True`:
                            we will fit a sparse encoder for you. The default value of `sparse_weight` is `{DEFAULT_SPARSE_WEIGHT}`.
                            """
                        )

            return model_name_or_path

    @staticmethod
    def __get_dictionary_path(model_name: str, dictionary_name_or_path: Optional[Union[str, Path]] = None) -> str:
        """
        Try to figure out what dictionary (depending on the model) the user wants
        """

        if model_name in STRING_MATCHING_MODELS and dictionary_name_or_path is None:
            raise ValueError("When using a string-matchin retriever you must specify `dictionary_name_or_path`!")

        if dictionary_name_or_path not in MODELS and dictionary_name_or_path not in ENTITY_TYPES:
            raise ValueError(
                f"""Unknown dictionary `{dictionary_name_or_path}`! \n
                    Available entity types are: {ENTITY_TYPES} \n
                    If you want to pass a local path please use the `Path` class, i.e. `model_name_or_path=Path(my_path)`
                    """
            )

        if dictionary_name_or_path is not None:
            if dictionary_name_or_path in ENTITY_TYPES:
                dictionary_name_or_path = ENTITY_TYPE_TO_NEL_DICTIONARY[dictionary_name_or_path]
            else:
                if model_name in MODEL_NAME_TO_NEL_DICTIONARY:
                    dictionary_name_or_path = MODEL_NAME_TO_NEL_DICTIONARY[dictionary_name_or_path]
                else:
                    raise ValueError(
                        """When using a custom model you need to specify a dictionary.
                        Available options are: 'disease', 'chemical', 'gene' and 'species'.
                        Or provide a path to a dictionary file."""
                    )

        return dictionary_name_or_path
