from typing import Iterable, Optional, Union

import numpy as np
import pandas as pd
from django.db import transaction
from django.db.models import Q

from core.models import User
from tournesol.models import (
    ContributorRating,
    ContributorRatingCriteriaScore,
    ContributorScaling,
    Entity,
    EntityCriteriaScore,
    Poll,
)
from tournesol.models.entity_score import ScoreMode


def save_entity_scores(
    poll,
    entity_scores: Union[pd.DataFrame, Iterable[tuple]],
    single_criteria=None,
    score_mode=ScoreMode.DEFAULT,
):
    if isinstance(entity_scores, pd.DataFrame):
        scores_iterator = entity_scores[
            ["entity_id", "criteria", "score", "uncertainty", "deviation"]
        ].itertuples(index=False)
    else:
        scores_iterator = entity_scores

    # Support scores iterator without deviation
    scores_iterator = (t if len(t) == 5 else t + (None,) for t in scores_iterator)

    with transaction.atomic():
        scores_to_delete = EntityCriteriaScore.objects.filter(
            poll=poll, score_mode=score_mode
        )
        if single_criteria:
            scores_to_delete = scores_to_delete.filter(criteria=single_criteria)
        scores_to_delete.delete()

        EntityCriteriaScore.objects.bulk_create(
            (
                EntityCriteriaScore(
                    poll=poll,
                    entity_id=entity_id,
                    criteria=criteria,
                    score=score,
                    uncertainty=uncertainty,
                    deviation=deviation,
                    score_mode=score_mode,
                )
                for entity_id, criteria, score, uncertainty, deviation in scores_iterator
            ),
            batch_size=10000,
        )


def save_tournesol_score_as_sum_of_criteria(poll):
    entities = []
    for entity in (
        Entity.objects.filter(all_criteria_scores__poll=poll)
        .distinct()
        .with_prefetched_scores(poll_name=poll.name)
    ):
        entity.tournesol_score = 10 * sum(
            criterion.score for criterion in entity.criteria_scores
        )
        entities.append(entity)
    Entity.objects.bulk_update(entities, ["tournesol_score"])


def save_contributor_scores(
    poll,
    contributor_scores,
    trusted_filter: Optional[bool] = None,
    single_criteria: Optional[str] = None,
    single_user_id: Optional[int] = None,
):
    if isinstance(contributor_scores, pd.DataFrame):
        scores_list = list(
            contributor_scores[
                ["user_id", "entity_id", "criteria", "score", "uncertainty"]
            ].itertuples(index=False)
        )
    else:
        scores_list = list(contributor_scores)

    ratings = ContributorRating.objects.filter(poll=poll)
    print("ratings")
    print(ratings)
    if single_user_id is not None:
        ratings = ratings.filter(user_id=single_user_id)
    print(ratings)

    rating_ids = {
        (contributor_id, video_id): rating_id
        for rating_id, contributor_id, video_id in ratings.values_list(
            "id", "user_id", "entity_id"
        )
    }
    print(rating_ids)
    ratings_to_create = set(
        (contributor_id, video_id)
        for contributor_id, video_id, _, _, _ in scores_list
        if (contributor_id, video_id) not in rating_ids
    )
    print(ratings_to_create)
    ContributorRating.objects.bulk_create(
        (
            ContributorRating(
                poll_id=poll.pk,
                entity_id=entity_id,
                user_id=contributor_id,
            )
            for contributor_id, entity_id in ratings_to_create
        ),
        ignore_conflicts=False,
    )
    # Refresh the `ratings_id` with the newly created `ContributorRating`s.
    rating_ids.update(
        {
            (contributor_id, entity_id): rating_id
            for rating_id, contributor_id, entity_id in ratings.values_list(
                "id", "user_id", "entity_id"
            )
        }
    )

    scores_to_delete = ContributorRatingCriteriaScore.objects.filter(
        contributor_rating__poll=poll
    )
    print(scores_to_delete)
    if trusted_filter is not None:
        trusted_query = Q(contributor_rating__user__in=User.trusted_users())
        scores_to_delete = scores_to_delete.filter(
            trusted_query if trusted_filter else ~trusted_query
        )

    if single_criteria is not None:
        scores_to_delete = scores_to_delete.filter(criteria=single_criteria)

    if single_user_id is not None:
        scores_to_delete = scores_to_delete.filter(
            contributor_rating__user_id=single_user_id
        )
    print("final")
    print(scores_to_delete)

    with transaction.atomic():
        scores_to_delete.delete()
        ContributorRatingCriteriaScore.objects.bulk_create(
            ContributorRatingCriteriaScore(
                contributor_rating_id=rating_ids[(contributor_id, video_id)],
                criteria=criteria,
                score=score,
                uncertainty=uncertainty,
            )
            for contributor_id, video_id, criteria, score, uncertainty in scores_list
        )


def insert_or_update_contributor_score(
    poll: Poll,
    entity_id: str,
    user_id: str,
    score: float,
    criteria: str,
    uncertainty: float,
):
    query_score_to_update = ContributorRatingCriteriaScore.objects.filter(
        contributor_rating__poll=poll,
        criteria=criteria,
        contributor_rating__user_id=user_id,
        contributor_rating__entity_id=entity_id,
    )
    print(score)
    print(type(score))
    contributor_rating_criteria_score = query_score_to_update.first()
    if contributor_rating_criteria_score:
        contributor_rating_criteria_score.score = score
        contributor_rating_criteria_score.uncertainty = uncertainty
        contributor_rating_criteria_score.save()
        print("save 1", entity_id, user_id, criteria, score, uncertainty)
    else:
        data = [(user_id, entity_id, criteria, score, uncertainty)]
        scores = pd.DataFrame(
            data, columns=["user_id", "entity_id", "criteria", "score", "uncertainty"]
        )
        print(data)
        save_contributor_scores(
            poll, scores, single_criteria=criteria, single_user_id=user_id
        )
        print("save 2", entity_id, user_id, criteria, score, uncertainty)


def save_contributor_scalings(poll: Poll, criteria: str, scalings: pd.DataFrame):
    scalings_iterator = (
        scalings[["s", "delta_s", "tau", "delta_tau"]]
        .replace({np.nan: None})
        .itertuples(index=True)
    )

    with transaction.atomic():
        ContributorScaling.objects.filter(poll=poll, criteria=criteria).delete()
        ContributorScaling.objects.bulk_create(
            (
                ContributorScaling(
                    poll=poll,
                    criteria=criteria,
                    user_id=user_id,
                    scale=s,
                    scale_uncertainty=delta_s,
                    translation=tau,
                    translation_uncertainty=delta_tau,
                )
                for user_id, s, delta_s, tau, delta_tau in scalings_iterator
            ),
            batch_size=10000,
        )
