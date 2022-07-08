import logging
import os
from functools import partial
from multiprocessing import Pool
from typing import Tuple

import numpy as np
import pandas as pd
from django import db

from core.models import User
from ml.inputs import MlInput, MlInputFromDb
from ml.outputs import (
    insert_or_update_contributor_score,
    save_entity_scores,
    save_tournesol_scores,
)
from tournesol.models import Entity, Poll
from tournesol.models.entity_score import ScoreMode
from tournesol.utils.constants import MEHESTAN_MAX_SCALED_SCORE

from .global_scores import apply_poll_scaling_on_global_scores, get_global_scores
from .poll_scaling import (
    apply_poll_scaling_on_global_scores,
    apply_poll_scaling_on_individual_scaled_scores,
)

logger = logging.getLogger(__name__)

R_MAX = MEHESTAN_MAX_SCALED_SCORE
ALPHA = 0.01  # Signal-to-noise hyperparameter


def get_new_scores_from_online_update(
    all_comparison_user: pd.DataFrame,
    id_entity_a: str,
    id_entity_b: str,
    previous_individual_raw_scores: pd.DataFrame,
) -> Tuple[float]:
    scores = all_comparison_user[["entity_a", "entity_b", "score"]]
    scores_sym = pd.concat(
        [
            scores,
            pd.DataFrame(
                {
                    "entity_a": scores.entity_b,
                    "entity_b": scores.entity_a,
                    "score": -1 * scores.score,
                }
            ),
        ]
    )

    # "Comparison tensor": matrix with all comparisons, values in [-R_MAX, R_MAX]
    r = scores_sym.pivot(index="entity_a", columns="entity_b", values="score")

    r_tilde = r / (1.0 + R_MAX)
    r_tilde2 = r_tilde**2

    # r.loc[a:b] is negative when a is prefered to b.
    l = -1.0 * r_tilde / np.sqrt(1.0 - r_tilde2)  # noqa: E741
    k = (1.0 - r_tilde2) ** 3

    L = k.mul(l).sum(axis=1)

    Kaa_np = np.array(k.sum(axis=1) + ALPHA)

    L_tilde = L / Kaa_np
    L_tilde_a = L_tilde[id_entity_a]
    L_tilde_b = L_tilde[id_entity_b]

    U_ab = -k / Kaa_np[:, None]
    U_ab = U_ab.fillna(0)

    if not previous_individual_raw_scores.index.isin([id_entity_a]).any():
        previous_individual_raw_scores.loc[id_entity_a] = 0.0

    if not previous_individual_raw_scores.index.isin([id_entity_b]).any():
        previous_individual_raw_scores.loc[id_entity_b] = 0.0

    dot_product = U_ab.dot(previous_individual_raw_scores)
    theta_star_a = (
        (L_tilde_a - dot_product[dot_product.index == id_entity_a].values)
        .squeeze()[()]
        .item()
    )
    theta_star_b = (
        (L_tilde_b - dot_product[dot_product.index == id_entity_b].values)
        .squeeze()[()]
        .item()
    )

    previous_individual_raw_scores.loc[id_entity_a] = theta_star_a
    previous_individual_raw_scores.loc[id_entity_b] = theta_star_b

    # Compute uncertainties
    scores_series = previous_individual_raw_scores.squeeze()
    scores_np = scores_series.to_numpy()
    theta_star_ab = pd.DataFrame(
        np.subtract.outer(scores_np, scores_np),
        index=scores_series.index,
        columns=scores_series.index,
    )
    K_diag = pd.DataFrame(
        data=np.diag(k.sum(axis=1) + ALPHA),
        index=k.index,
        columns=k.index,
    )
    sigma2 = (1.0 + (np.nansum(k * (l - theta_star_ab) ** 2) / 2)) / len(scores)

    delta_star = pd.Series(
        np.sqrt(sigma2) / np.sqrt(np.diag(K_diag)), index=K_diag.index
    )
    delta_star_a = delta_star[id_entity_a]
    delta_star_b = delta_star[id_entity_b]

    return (theta_star_a, delta_star_a, theta_star_b, delta_star_b)


def _run_online_heuristics_for_criterion(
    criteria: str,
    ml_input: MlInput,
    uid_a: str,
    uid_b: str,
    user_id: str,
    poll_pk: int,
    delete_comparison_case: bool,
):
    """
    This function apply the online heuristics for a criteria. There is 3 cases :
    1. a new comparison has been made
    2. a comparison has been updated
    3. a comparison has been deleted

    * For each case, this function need to know which entities
        are being concerned as input and what to do
    * For each case, we first check if the input are compliant
        with the data (check_requirements_are_good_for_online_heuristics)
    * For each case, then we read the previous raw scores
        (compute_and_update_individual_scores_online_heuristics) and compute new scores
    * For each case, we reapply scaling (individual) from previous scale
    * For each case, we compute new global scores for the two entities
        and we apply poll level scaling at global scores

    """
    poll = Poll.objects.get(pk=poll_pk)
    all_comparison_user = ml_input.get_comparisons(criteria=criteria, user_id=user_id)
    entity_id_a = Entity.objects.get(uid=uid_a).pk
    entity_id_b = Entity.objects.get(uid=uid_b).pk
    if not check_requirements_are_good_for_online_heuristics(
        criteria, all_comparison_user, entity_id_a, entity_id_b, delete_comparison_case
    ):
        return
    compute_and_update_individual_scores_online_heuristics(
        criteria,
        ml_input,
        user_id,
        poll,
        all_comparison_user,
        entity_id_a,
        entity_id_b,
        delete_comparison_case,
    )

    partial_scaled_scores_for_ab = apply_scaling_on_individual_scores_online_heuristics(
        criteria, ml_input, entity_id_a, entity_id_b
    )

    if not partial_scaled_scores_for_ab.empty:
        calculate_global_scores_in_all_score_mode(
            criteria, poll, partial_scaled_scores_for_ab
        )
        apply_poll_scaling_on_individual_scaled_scores(
            poll, partial_scaled_scores_for_ab
        )


def calculate_global_scores_in_all_score_mode(
    criteria: str,
    poll: Poll,
    df_partial_scaled_scores_for_ab: pd.DataFrame,
):
    for mode in ScoreMode:
        global_scores = get_global_scores(
            df_partial_scaled_scores_for_ab, score_mode=mode
        )
        global_scores["criteria"] = criteria

        apply_poll_scaling_on_global_scores(poll, global_scores)

        save_entity_scores(
            poll,
            global_scores,
            single_criteria=criteria,
            score_mode=mode,
            delete_all=False,
        )


def apply_scaling_on_individual_scores_online_heuristics(
    criteria: str, ml_input: MlInput, entity_id_a: int, entity_id_b: int
):
    all_user_scalings = ml_input.get_user_scalings()
    all_indiv_score_a = ml_input.get_indiv_score(
        entity_id=entity_id_a, criteria=criteria
    )
    if all_indiv_score_a.empty:
        logger.warning(
            "_run_online_heuristics_for_criterion :  \
            no individual score found for '%s' and criteria '%s'",
            entity_id_a,
            criteria,
        )
        return pd.DataFrame()
    all_indiv_score_b = ml_input.get_indiv_score(
        entity_id=entity_id_b, criteria=criteria
    )
    if all_indiv_score_b.empty:
        logger.warning(
            "_run_online_heuristics_for_criterion :  \
            no individual score found for '%s' and criteria '%s'",
            entity_id_b,
            criteria,
        )
        return pd.DataFrame()
    all_indiv_score = pd.concat([all_indiv_score_a, all_indiv_score_b])

    df = all_indiv_score.merge(
        ml_input.get_ratings_properties(), how="inner", on=["user_id", "entity_id"]
    )
    df["is_public"].fillna(False, inplace=True)
    df["is_trusted"].fillna(False, inplace=True)
    df["is_supertrusted"].fillna(False, inplace=True)

    df = df.merge(all_user_scalings, how="left", on="user_id")
    df["scale"].fillna(1, inplace=True)
    df["translation"].fillna(0, inplace=True)
    df["scale_uncertainty"].fillna(0, inplace=True)
    df["translation_uncertainty"].fillna(0, inplace=True)
    df["uncertainty"] = (
        df["scale"] * df["raw_uncertainty"]
        + df["scale_uncertainty"] * df["raw_score"].abs()
        + df["translation_uncertainty"]
    )
    df["score"] = df["raw_score"] * df["scale"] + df["translation"]
    df.drop(
        ["scale", "translation", "scale_uncertainty", "translation_uncertainty"],
        axis=1,
        inplace=True,
    )
    return df


def check_requirements_are_good_for_online_heuristics(
    criteria: str,
    df_all_comparison_user: pd.DataFrame,
    entity_id_a: int,
    entity_id_b: int,
    delete_comparison_case: bool,
):
    if df_all_comparison_user.empty:
        logger.warning(
            "_run_online_heuristics_for_criterion : no comparison  for criteria '%s'",
            criteria,
        )
        return False
    if (
        df_all_comparison_user[
            (df_all_comparison_user.entity_a == entity_id_a)
            & (df_all_comparison_user.entity_b == entity_id_b)
        ].empty
        and df_all_comparison_user[
            (df_all_comparison_user.entity_a == entity_id_b)
            & (df_all_comparison_user.entity_b == entity_id_a)
        ].empty
    ):
        if not delete_comparison_case:
            logger.warning(
                "_run_online_heuristics_for_criterion :  \
                no comparison found for '%s' with '%s' and criteria '%s'",
                entity_id_a,
                entity_id_b,
                criteria,
            )
            return False
    return True


def compute_and_update_individual_scores_online_heuristics(
    criteria: str,
    ml_input: MlInput,
    user_id: int,
    poll: Poll,
    df_all_comparison_user: pd.DataFrame,
    entity_id_a: int,
    entity_id_b: int,
    delete_comparison_case: bool = False,
):
    """
    this function apply the online heuristics to raw score for the user and the concerned entities
    1. we get all the previous scores from the user and the criteria
    2. we compute the new raw_score and raw_uncertainty (get_new_scores_from_online_update)
    3. we save the new raw_score
    """
    previous_individual_raw_scores = ml_input.get_indiv_score(
        user_id=user_id, criteria=criteria
    )
    previous_individual_raw_scores = previous_individual_raw_scores[
        ["entity_id", "raw_score"]
    ]
    previous_individual_raw_scores = previous_individual_raw_scores.set_index(
        "entity_id"
    )
    (
        theta_star_a,
        delta_star_a,
        theta_star_b,
        delta_star_b,
    ) = get_new_scores_from_online_update(
        df_all_comparison_user, entity_id_a, entity_id_b, previous_individual_raw_scores
    )

    insert_or_update_contributor_score(
        poll=poll,
        entity_id=entity_id_a,
        user_id=user_id,
        raw_score=theta_star_a,
        criteria=criteria,
        raw_uncertainty=delta_star_a,
    )
    insert_or_update_contributor_score(
        poll=poll,
        entity_id=entity_id_b,
        user_id=user_id,
        raw_score=theta_star_b,
        criteria=criteria,
        raw_uncertainty=delta_star_b,
    )


def run_online_heuristics(
    ml_input: MlInput,
    uid_a: str,
    uid_b: str,
    user_id: str,
    poll: Poll,
    delete_comparison_case: bool,
    parallel_computing: bool = True,
):
    """
    This function use multiprocessing.

        1. Always close all database connections in the main process before
           creating forks. Django will automatically re-create new database
           connections when needed.

        2. Do not pass Django model's instances as arguments to the function
           run by child processes. Using such instances in child processes
           will raise an exception: connection already closed.

        3. Do not fork the main process within a code block managed by
           a single database transaction.

    See the indications to close the database connections:
        - https://www.psycopg.org/docs/usage.html#thread-and-process-safety
        - https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNECT

    See how django handles database connections:
        - https://docs.djangoproject.com/en/4.0/ref/databases/#connection-management
    """
    logger.info("Online Heuristic Mehestan for poll '%s': Start", poll.name)

    # Avoid passing model's instances as arguments to the function run by the
    # child processes. See this method docstring.
    poll_pk = poll.pk
    criteria = poll.criterias_list

    partial_online_heuristics = partial(
        _run_online_heuristics_for_criterion,
        ml_input=ml_input,
        poll_pk=poll_pk,
        uid_a=uid_a,
        uid_b=uid_b,
        user_id=user_id,
        delete_comparison_case=delete_comparison_case,
    )
    if parallel_computing:
        os.register_at_fork(before=db.connections.close_all)

        # compute each criterion in parallel
        with Pool(processes=max(1, os.cpu_count() - 1)) as pool:
            for _ in pool.imap_unordered(
                partial_online_heuristics,
                criteria,
            ):
                pass
    else:
        for criterion in criteria:
            logger.info(
                "Sequential Online Heuristic Mehestan  \
                for poll '%s  for criterion '%s': Start ",
                poll.name,
                criterion,
            )

            partial_online_heuristics(criterion)

    save_tournesol_scores(poll)
    logger.info("Online Heuristic Mehestan for poll '%s': Done", poll.name)


def update_user_scores(
    poll: Poll, user: User, uid_a: str, uid_b: str, delete_comparison_case: bool
):
    ml_input = MlInputFromDb(poll_name=poll.name)
    run_online_heuristics(
        ml_input=ml_input,
        uid_a=uid_a,
        uid_b=uid_b,
        user_id=user.pk,
        poll=poll,
        delete_comparison_case=delete_comparison_case,
        parallel_computing=False,
    )
