import React from 'react';
import { useTranslation } from 'react-i18next';
import { Grid } from '@mui/material';

import { ContentBox, ContentHeader } from 'src/components';

import { PersonalVouchersProvider } from './context';
import CreateVoucherForm from './CreateVoucherForm';
import GivenVouchers from './GivenVouchers';
import ReceivedVouchers from './ReceivedVouchers';

const PersonalVouchersPage = () => {
  const { t } = useTranslation();

  return (
    <PersonalVouchersProvider>
      <ContentHeader title={t('personalMenu.vouching')} />
      <ContentBox maxWidth="lg">
        <CreateVoucherForm />
        <Grid container gap={2} justifyContent="space-between">
          <Grid item md={6}>
            <GivenVouchers />
          </Grid>
          <Grid item md={5}>
            <ReceivedVouchers />
          </Grid>
        </Grid>
      </ContentBox>
    </PersonalVouchersProvider>
  );
};

export default PersonalVouchersPage;
