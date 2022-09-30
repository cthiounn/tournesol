from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd
from django.db.models import Case, F, QuerySet, When
from django.db.models.expressions import RawSQL
from django.db.models import Q
from core.models import User
from tournesol.models import (
    ComparisonCriteriaScore,
    ContributorRating,
    ContributorRatingCriteriaScore,
    ContributorScaling,
    Entity,
)
import logging
import timeit

logger = logging.getLogger("toto")


class MlInput(ABC):
    @abstractmethod
    def get_comparisons(
        self,
        trusted_only=False,
        criteria: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> pd.DataFrame:
        """Fetch data about comparisons submitted by users

        Returns:
        - comparisons_df: DataFrame with columns
            * `user_id`: int
            * `entity_a`: int or str
            * `entity_b`: int or str
            * `criteria`: str
            * `score`: float
            * `weight`: float
        """
        pass

    @abstractmethod
    def get_ratings_properties(self) -> pd.DataFrame:
        """Fetch data about contributor ratings properties

        Returns:
        - ratings_df: DataFrame with columns
            * `user_id`: int
            * `entity_id`: int or str
            * `is_public`: bool
            * `is_trusted`: bool
            * `is_supertrusted`: bool
        """
        pass

    @abstractmethod
    def get_user_scalings(
        self, user_id: Optional[int] = None, criteria: Optional[str] = None
    ) -> pd.DataFrame:
        pass

    @abstractmethod
    def get_indiv_score(
        self,
        criteria: Optional[str] = None,
        entity_id: Optional[str] = None,
        user_id: Optional[int] = None,
        entity_id_in=None
    ) -> pd.DataFrame:
        pass


class MlInputFromPublicDataset(MlInput):
    def __init__(self, csv_file):
        self.public_dataset = pd.read_csv(csv_file)
        self.public_dataset.rename(
            {"video_a": "entity_a", "video_b": "entity_b"}, axis=1, inplace=True
        )
        self.public_dataset["user_id"], self.user_indices = self.public_dataset[
            "public_username"
        ].factorize()

    def get_comparisons(
        self, trusted_only=False, criteria=None, user_id=None
    ) -> pd.DataFrame:
        df = self.public_dataset.copy(deep=False)
        if criteria is not None:
            df = df[df.criteria == criteria]
        if user_id is not None:
            df = df[df.user_id == user_id]
        return df[["user_id", "entity_a", "entity_b", "criteria", "score", "weight"]]

    def get_ratings_properties(self):
        user_entities_pairs = pd.Series(
            iter(
                set(self.public_dataset.groupby(["user_id", "entity_a"]).indices.keys())
                | set(
                    self.public_dataset.groupby(["user_id", "entity_b"]).indices.keys()
                )
            )
        )
        df = pd.DataFrame([*user_entities_pairs], columns=["user_id", "entity_id"])
        df["is_public"] = True
        top_users = df.value_counts("user_id").index[:6]
        df["is_trusted"] = df["is_supertrusted"] = df["user_id"].isin(top_users)
        return df

    def get_user_scalings(self, user_id=None, criteria=None):
        return

    def get_indiv_score(
        self, criteria=None, user_id=None, entity_id=None,entity_id_in=None
    ) -> pd.DataFrame:
        return

from django.db import connection


class MlInputFromDb(MlInput):
    SUPERTRUSTED_MIN_ENTITIES_TO_COMPARE = 20
    MAX_SUPERTRUSTED_USERS = 100

    def __init__(self, poll_name: str):
        self.poll_name = poll_name

    def get_supertrusted_users(self,django_orm_union=True) -> QuerySet[User]:

        if django_orm_union:
            n_alternatives = (
                Entity.objects.filter(comparisons_entity_1__poll__name=self.poll_name)
                .union(
                    Entity.objects.filter(comparisons_entity_2__poll__name=self.poll_name)
                )
                .count()
            )
        else:
            with connection.cursor() as cursor:
                cursor.execute("""
                select count(DISTINCT(euid)) FROM
                    (
                    select distinct(entity_1_id) as euid from tournesol_comparison 
                    where poll_id=(select p.id from tournesol_poll p
                        WHERE p.name = %s) 
                    union 
                    select distinct(entity_2_id) as euid from tournesol_comparison 
                    where poll_id=(select p.id from tournesol_poll p
                        WHERE p.name = %s) 
                    ) as subquery
                """,(self.poll_name,self.poll_name,),)
                row = cursor.fetchone()
                n_alternatives = row[0]
        users = User.objects.alias(
            n_compared_entities=RawSQL(
                """
                SELECT COUNT(DISTINCT e.id)
                FROM tournesol_entity e
                INNER JOIN tournesol_comparison c
                    ON (c.entity_1_id = e.id OR c.entity_2_id = e.id)
                INNER JOIN tournesol_poll p
                    ON (p.id = c.poll_id AND p.name = %s)
                WHERE c.user_id = "core_user"."id"
                """,
                (self.poll_name,),
            )
        )

        if n_alternatives <= self.SUPERTRUSTED_MIN_ENTITIES_TO_COMPARE:
            # The number of alternatives is low enough to consider as supertrusted
            # all trusted users who have compared all alternatives.
            have_compared_all_alternatives = users.filter(
                n_compared_entities__gte=n_alternatives
            )
            return User.trusted_users().filter(pk__in=have_compared_all_alternatives)


        n_supertrusted_seed = User.supertrusted_seed_users().count()

        return User.supertrusted_seed_users().union(
            users.filter(
                pk__in=User.trusted_users(),
                n_compared_entities__gte=self.SUPERTRUSTED_MIN_ENTITIES_TO_COMPARE,
            )
            .exclude(pk__in=User.supertrusted_seed_users())
            .order_by("-n_compared_entities")[
                : self.MAX_SUPERTRUSTED_USERS - n_supertrusted_seed
            ]
        )

    def get_comparisons(
        self, trusted_only=False, criteria=None, user_id=None
    ) -> pd.DataFrame:
        scores_queryset = ComparisonCriteriaScore.objects.filter(
            comparison__poll__name=self.poll_name
        )
        if criteria is not None:
            scores_queryset = scores_queryset.filter(criteria=criteria)

        if trusted_only:
            scores_queryset = scores_queryset.filter(
                comparison__user__in=User.trusted_users()
            )

        if user_id is not None:
            scores_queryset = scores_queryset.filter(comparison__user_id=user_id)

        values = scores_queryset.values(
            "score",
            "criteria",
            "weight",
            entity_a=F("comparison__entity_1_id"),
            entity_b=F("comparison__entity_2_id"),
            user_id=F("comparison__user_id"),
        )
        if len(values) > 0:
            df = pd.DataFrame(values)
            return df[
                ["user_id", "entity_a", "entity_b", "criteria", "score", "weight"]
            ]

        return pd.DataFrame(
            columns=[
                "user_id",
                "entity_a",
                "entity_b",
                "criteria",
                "score",
                "weight",
            ]
        )

    def get_ratings_properties(self):
        trusted_users = User.trusted_users()
        supertrusted_users = self.get_supertrusted_users(django_orm_union=False).values("id")

        annoted_contribratings = ContributorRating.objects.filter(
            poll__name=self.poll_name,
        ).annotate(
            is_trusted=Case(When(user__in=trusted_users, then=True), default=False),
            is_supertrusted=Case(
                When(user__in=supertrusted_users, then=True),
                default=False,
            ),
        )

        values = annoted_contribratings.values_list(
            "user_id",
            "entity_id",
            "is_public",
            "is_trusted",
            "is_supertrusted",
        )

        if len(values) == 0:
            return pd.DataFrame(
                columns=[
                    "user_id",
                    "entity_id",
                    "is_public",
                    "is_trusted",
                    "is_supertrusted",
                ]
            )
        return pd.DataFrame.from_records(values,columns=[
                    "user_id",
                    "entity_id",
                    "is_public",
                    "is_trusted",
                    "is_supertrusted",
                ])

    def get_user_scalings(self, user_id=None) -> pd.DataFrame:
        """Fetch saved invidiual scalings
        Returns:
        - ratings_df: DataFrame with columns
            * `user_id`: int
            * `criteria`: str
            * `scale`: float
            * `scale_uncertainty`: float
            * `translation`: float
            * `translation_uncertainty`: float
        """

        scalings = ContributorScaling.objects.filter(poll__name=self.poll_name)
        if user_id is not None:
            scalings = scalings.filter(user_id=user_id)
        values = scalings.values(
            "user_id",
            "criteria",
            "scale",
            "scale_uncertainty",
            "translation",
            "translation_uncertainty",
        )
        if len(values) == 0:
            return pd.DataFrame(
                columns=[
                    "user_id",
                    "criteria",
                    "scale",
                    "scale_uncertainty",
                    "translation",
                    "translation_uncertainty",
                ]
            )
        return pd.DataFrame(values)

    def get_indiv_score(
        self, criteria=None, user_id=None, entity_id=None,entity_id_in=None
    ) -> pd.DataFrame:

        scores_queryset = ContributorRatingCriteriaScore.objects.filter(
            contributor_rating__poll__name=self.poll_name
        )
        if criteria is not None:
            scores_queryset = scores_queryset.filter(criteria=criteria)

        if user_id is not None:
            scores_queryset = scores_queryset.filter(
                contributor_rating__user_id=user_id
            )

        if entity_id is not None:
            scores_queryset = scores_queryset.filter(
                contributor_rating__entity_id=entity_id
            )

        if entity_id_in is not None:
            scores_queryset = scores_queryset.filter(
                contributor_rating__entity_id__in=entity_id_in
            )
        values = scores_queryset.values(
            "raw_score",
            "raw_uncertainty",
            user_id=F("contributor_rating__user_id"),
            entity_id=F("contributor_rating__entity_id"),
        )
        if len(values) > 0:
            df = pd.DataFrame(values)
            return df[
                [
                    "user_id",
                    "entity_id",
                    "raw_score",
                    "raw_uncertainty",
                ]
            ]

        return pd.DataFrame(
            columns=[
                "user_id",
                "entity_id",
                "raw_score",
                "raw_uncertainty",
            ]
        )
